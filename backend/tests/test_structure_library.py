from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest
from fastapi.testclient import TestClient

from test_auth_isolation import auth_database, auth_app, activate, login
from app.auth.cli import manage_user
from app.auth.schema import validate_isolation_schema
from app.postgres_database import postgres_connection
from app.postgres_migrations import apply_postgres_migrations
from app.routers.structure_library import router

PREFIX = '/api/v1/structure-library'


def payload(smiles='*CC*', name='候选 A'):
    return dict(name=name, smiles=smiles, ket={'root': {'nodes': [{'$ref': 'mol0'}]},
        'mol0': {'type': 'molecule', 'atoms': [{'label': 'C', 'location': [2, 4, 0], 'selected': True}], 'bonds': []}},
        editor_version='3.8.0', snapshot_schema_version=1, client_request_id=str(uuid4()))


@pytest.fixture
def library_app(auth_app, auth_database):
    apply_postgres_migrations(auth_database['admin'], allowed_kinds={'expand'})
    app, user, client = auth_app
    app.state.settings = SimpleNamespace(app_postgres_dsn=auth_database['api'], structure_library_enabled=True)
    app.state.postgres_connection_factory = postgres_connection
    app.include_router(router)
    activate(client, user)
    return app, user, client


def test_roundtrip_paging_and_immutable_content(library_app):
    app, user, client = library_app
    assert client.get(PREFIX+'/status').json()['available']
    ids = []
    for smiles in ('*CC*', '[*]CC[*]', 'CCO.CCN', '[13CH3][C@H](O)C(=O)[O-]'):
        response = client.post(PREFIX+'/records', json=payload(smiles))
        assert response.status_code == 201, response.text
        saved = response.json()
        ids.append(saved['id'])
        assert saved['smiles'] == smiles
        assert 'selected' not in saved['ket']['mol0']['atoms'][0]
        assert saved['ket']['mol0']['atoms'][0]['location'] == [2, 4, 0]
        assert saved['preview_svg']
    first = client.get(PREFIX+'/records?limit=2').json()
    assert len(first['items']) == 2 and 'ket' not in first['items'][0]
    second = client.get(PREFIX+'/records', params={'limit': 2, 'cursor': first['next_cursor']}).json()
    assert {r['id'] for r in first['items']+second['items']} == set(ids)
    path = PREFIX+'/records/'+ids[0]
    assert client.patch(path, json={'name':'改名','metadata_revision':1}).json()['metadata_revision'] == 2
    assert client.patch(path, json={'name':'旧窗口','metadata_revision':1}).status_code == 409
    assert client.patch(path, json={'name':'覆盖','metadata_revision':2,'smiles':'N'}).status_code == 422
    assert client.get(path).json()['smiles'] == '*CC*'
    assert len(client.get(PREFIX+'/records', params={'q':'改名'}).json()['items']) == 1
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404
    app.state.settings.structure_library_enabled = False
    assert not client.get(PREFIX+'/status').json()['available']
    assert client.get(PREFIX+'/records').status_code == 503


def test_retry_identity_quota_and_validation(library_app, monkeypatch):
    app, user, client = library_app
    body = payload()
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: client.post(PREFIX+'/records', json=body), range(2)))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert responses[0].json()['id'] == responses[1].json()['id']
    assert client.post(PREFIX+'/records', json={**body,'name':'different'}).status_code == 409
    assert client.post(PREFIX+'/records', json=payload('invalid')).status_code == 422
    assert client.post(PREFIX+'/records', json={**payload(), 'ket': {'root': {'nodes': []}}}).status_code == 422
    invalid = payload(); invalid['ket']['root']['nodes'][0]['$ref'] = 'absent'
    assert client.post(PREFIX+'/records', json=invalid).status_code == 422
    oversized = payload(); oversized['ket']['annotation'] = 'x' * (1024 * 1024)
    assert client.post(PREFIX+'/records', json=oversized).status_code == 413
    assert client.post(PREFIX+'/records', json={**payload(),'owner_user_id':str(user['user_id'])}).status_code == 422
    monkeypatch.setenv('PRIVATE_STRUCTURE_LIBRARY_MAX_ROWS', '1')
    assert client.post(PREFIX+'/records', json=payload()).status_code == 429
    assert client.post(PREFIX+'/records', json=body).status_code == 201
    assert client.get(PREFIX+'/records?cursor=bad').status_code == 422
    assert client.post(PREFIX+'/records', content=b'x'*(2*1024*1024+1)).status_code == 413


def test_real_account_and_sql_isolation(library_app, auth_database):
    app, a, client = library_app
    record = client.post(PREFIX+'/records',json=payload()).json()
    path = PREFIX+'/records/'+record['id']
    other_browser = TestClient(app)
    login(other_browser, a, 'changed-test-password')
    assert other_browser.get(path).json()['id'] == record['id']
    with psycopg.connect(auth_database['admin'], row_factory=dict_row) as connection:
        b = manage_user(connection, 'create', username='library-'+uuid4().hex, password='initial-test-password')
    b_client = TestClient(app); activate(b_client,b)
    assert b_client.get(PREFIX+'/records').json()['items'] == []
    assert b_client.get(path).status_code == 404
    assert b_client.patch(path,json={'name':'stolen','metadata_revision':1}).status_code == 404
    assert b_client.delete(path).status_code == 404
    assert TestClient(app).get(PREFIX+'/records').status_code == 401
    with psycopg.connect(auth_database['api'], row_factory=dict_row) as connection:
        assert connection.execute('SELECT count(*) AS n FROM structure_library.records').fetchone()['n'] == 0
        from app.services.structure_library import save_record, CreateStructureRecord
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                save_record(connection, str(a['user_id']), CreateStructureRecord(**payload()))
        connection.execute("SELECT set_config('app.user_id',%s,true)",(str(a['user_id']),))
        assert connection.execute('SELECT count(*) AS n FROM structure_library.records').fetchone()['n'] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("UPDATE structure_library.records SET smiles='N'")
    with psycopg.connect(auth_database['admin'], row_factory=dict_row) as connection:
        assert any(p['relation']=='structure_library.records' for p in validate_isolation_schema(connection)['rls_policies'])
        from app.auth.cutover import backup_state_seal
        assert backup_state_seal(connection)['structure_library.records']['rows'] >= 1
        from scripts.user_isolation_audit import capture
    with psycopg.connect(auth_database['audit'], row_factory=dict_row) as connection:
        evidence = capture(connection)
        assert evidence['schema_version'] == 3
        assert 'structure_library.records' in evidence['business_tables']
        assert 'structure_library' in evidence['backup_required_schemas']


def test_0020_release_preflight_and_tamper_rejection(library_app, auth_database):
    from app.config import Settings
    from app.postgres_preflight import run_preflight, SCHEMA_TARGET_STRUCTURE_LIBRARY
    from app.auth.isolation_ledger import validate_service_migration_ledger
    settings = Settings(app_postgres_dsn=auth_database['api'], pi_postgres_dsn=auth_database['api'],
        model_enabled=False, gen_model_enabled=False, retro_model_enabled=False, gpu_broker_enabled=False)
    report = run_preflight(settings, dsn=auth_database['service'], mode='schema', strict=True,
        schema_target=SCHEMA_TARGET_STRUCTURE_LIBRARY)
    assert report['current_readiness'], report['strict_errors']
    with psycopg.connect(auth_database['admin'], row_factory=dict_row) as connection:
        connection.execute("GRANT UPDATE(smiles) ON structure_library.records TO nexpoly_api")
        with pytest.raises(ValueError, match='immutable'):
            validate_isolation_schema(connection)
        connection.rollback()

        connection.execute("ALTER POLICY owner_access ON structure_library.records USING (true)")
        with pytest.raises(ValueError, match='RLS policies'):
            validate_isolation_schema(connection)
        connection.rollback()
        connection.execute("UPDATE governance.schema_migrations SET checksum=%s WHERE version='0020_structure_library'", ('0'*64,))
        with pytest.raises(ValueError, match='canonical ledger'):
            validate_service_migration_ledger(connection)
        connection.rollback()


def test_full_backup_restore_keeps_saved_records(library_app, auth_database, tmp_path):
    from app.auth.backup import backup_and_verify
    app, user, client = library_app
    record = client.post(PREFIX+'/records', json=payload('CCN', '备份恢复结构')).json()
    target_name = 'library_restore_' + uuid4().hex[:12]
    target = make_conninfo(auth_database['admin'], dbname=target_name)
    with psycopg.connect(auth_database['admin'], autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(target_name)))
    try:
        receipt = backup_and_verify(auth_database['admin'], target, tmp_path/'private-backup')
        proof = json.loads(receipt.read_text())
        assert proof['restore_verified']
        assert proof['backup_state']['structure_library.records']['rows'] >= 1
        with psycopg.connect(target, row_factory=dict_row) as connection:
            restored = connection.execute('SELECT smiles,ket,owner_user_id FROM structure_library.records WHERE id=%s', (record['id'],)).fetchone()
            assert restored['smiles'] == record['smiles']
            assert restored['ket'] == record['ket']
            assert str(restored['owner_user_id']) == str(user['user_id'])
    finally:
        with psycopg.connect(auth_database['admin'], autocommit=True) as connection:
            connection.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(target_name)))


def test_static_rebuild_cannot_include_structure_library(monkeypatch):
    from app.import_postgres import STATIC_REBUILD_TABLES_BY_DATASET, truncate_static_import_tables
    monkeypatch.setitem(STATIC_REBUILD_TABLES_BY_DATASET, 'core', (('structure_library', 'records'),))
    with pytest.raises(ValueError, match='business-mutable boundary'):
        truncate_static_import_tables(None, {'core'})


@pytest.mark.parametrize('ket', [
    {'root': {'nodes': [None]}, 'mol0': {'type': 'molecule', 'atoms': [{'label': 'C'}]}},
    {'root': {'nodes': [{'$ref': 'annotation'}]}, 'annotation': {'type': 'text'},
     'orphan': {'type': 'molecule', 'atoms': [{'label': 'C'}]}},
    {'root': {'nodes': [{'$ref': 'mol0'}]}, 'mol0': 'not-a-node',
     'orphan': {'type': 'molecule', 'atoms': [{'label': 'C'}]}},
])
def test_rejects_invalid_or_atomless_root_documents(library_app, ket):
    _app, _user, client = library_app
    response = client.post(PREFIX+'/records', json={**payload('C'), 'ket': ket})
    assert response.status_code == 422, response.text


@pytest.mark.parametrize('enabled', [False, True])
def test_full_application_starts_on_0020_with_feature_on_or_off(library_app, auth_database, monkeypatch, enabled):
    from app.config import Settings
    from app.main import create_app
    monkeypatch.setenv('AUTH_POSTGRES_DSN', auth_database['auth'])
    monkeypatch.setenv('APP_SERVICE_POSTGRES_DSN', auth_database['service'])
    monkeypatch.setenv('AUTH_COOKIE_SECURE', 'false')
    monkeypatch.setenv('STRUCTURE_LIBRARY_ENABLED', str(enabled).lower())
    app = create_app(Settings(app_postgres_dsn=auth_database['api'], pi_postgres_dsn=auth_database['api'],
        allowed_origins='http://testserver',
        model_enabled=False, gen_model_enabled=False, retro_model_enabled=False,
        gpu_preload_mode='lazy', gpu_broker_enabled=False))
    with TestClient(app):
        assert set(app.state.database_preflight_errors) == {
            'Property filter records are empty; run the property_filter import before deployment.',
            'Required runtime source is missing: property_filter_csv',
            'Required Postgres analytics snapshot is missing or invalid',
        }
        app.state.auth.assert_application_ready()


def test_current_runtime_target_resolves_0020_and_rejects_unknown_ledgers(library_app, auth_database):
    from app.config import Settings
    from app.postgres_preflight import run_preflight, SCHEMA_TARGET_CURRENT_ISOLATION, SCHEMA_TARGET_STRUCTURE_LIBRARY
    settings = Settings(app_postgres_dsn=auth_database['api'], pi_postgres_dsn=auth_database['api'],
        model_enabled=False, gen_model_enabled=False, retro_model_enabled=False, gpu_broker_enabled=False)
    def proof():
        return run_preflight(settings, dsn=auth_database['service'], mode='schema', strict=True,
            schema_target=SCHEMA_TARGET_CURRENT_ISOLATION)
    report = proof()
    assert report['current_readiness'], report['strict_errors']
    assert report['schema_target'] == SCHEMA_TARGET_STRUCTURE_LIBRARY
    try:
        with psycopg.connect(auth_database['admin']) as connection:
            connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES('9999_unknown',%s)", ('0'*64,))
        report = proof()
        assert not report['current_readiness'] and not report['strict_ok']
    finally:
        with psycopg.connect(auth_database['admin']) as connection:
            connection.execute("DELETE FROM governance.schema_migrations WHERE version='9999_unknown'")


def test_roundtrips_real_ketcher_bond_and_annotation_document(library_app):
    from pathlib import Path
    _app, _user, client = library_app
    ket = json.loads((Path(__file__).parent/'fixtures/structure_library_ketcher38.json').read_text())
    response = client.post(PREFIX+'/records', json={**payload('CCO'), 'ket': ket})
    assert response.status_code == 201, response.text
    saved = response.json()
    assert saved['ket'] == ket
    assert saved['ket']['mol0']['bonds'][0]['atoms'] == [0, 1]
    assert 'Real Postgres layout A' in json.dumps(saved['ket'])
