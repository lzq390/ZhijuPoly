"""Opt-in PostgreSQL 16 tests; each run creates and drops its own empty database.

Set NEXPOLY_ISOLATION_TEST_DSN to an isolated test administrator connection.
No existing database or deployment data is changed.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
import pytest

from app.auth.context import Identity, current_owner_id, is_service_context, service_context, user_context
from app.services.online_knowledge import postgres_history_repository as history
from app.services.polymerization_batch import repository as batch_repository
from app.services.polymerization_batch.models import BatchError, BatchSettings

A = Identity("11111111-1111-1111-1111-111111111111")
B = Identity("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def isolated_assets_db(monkeypatch, tmp_path):
    administrator = os.getenv("NEXPOLY_ISOLATION_TEST_DSN")
    if not administrator:
        pytest.skip("explicit isolated PostgreSQL test administrator is required")
    name = "isolation_assets_" + uuid4().hex
    api_login, service_login = "assets_api_" + uuid4().hex, "assets_service_" + uuid4().hex
    password = uuid4().hex
    with psycopg.connect(administrator, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    dsn = make_conninfo(administrator, dbname=name)
    try:
        with psycopg.connect(dsn) as connection:
            migrations = Path(__file__).parents[1] / "migrations" / "postgres"
            for path in sorted(migrations.glob("*.sql")):
                if path.name.startswith("0018"):
                    connection.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,'alice','!',false),(%s,'bob','!',false)", (A.user_id, B.user_id))
                    connection.execute("SELECT set_config('nexpoly.legacy_owner',%s,true)", (A.user_id,))
                connection.execute(path.read_text())
        with psycopg.connect(administrator, autocommit=True) as admin:
            for login, group in ((api_login, "nexpoly_api"), (service_login, "nexpoly_service")):
                admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(sql.Identifier(login), sql.Literal(password)))
                admin.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(group), sql.Identifier(login)))

        @contextmanager
        def connection_factory(_dsn=None):
            login = service_login if is_service_context() else api_login
            actual_dsn = make_conninfo(dsn, user=login, password=password)
            with psycopg.connect(actual_dsn, row_factory=dict_row) as connection:
                role = connection.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
                assert not role["rolsuper"] and not role["rolbypassrls"]
                if not is_service_context():
                    connection.execute("SELECT set_config('app.user_id',%s,true)", (current_owner_id(),))
                yield connection

        monkeypatch.setattr(batch_repository, "postgres_connection", connection_factory)
        repository = batch_repository.BatchRepository(dsn, BatchSettings(enabled=True, storage_root=tmp_path))
        yield dsn, connection_factory, repository
    finally:
        with psycopg.connect(administrator, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            for login in (api_login, service_login):
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))


def _submit(repository, owner, key="shared-key"):
    with user_context(owner), service_context(), repository.connection() as connection:
        previous = repository.admit(connection, key, "same-hash", owner_user_id=current_owner_id())
        if previous:
            return previous
        return repository.insert_job(connection, uuid4().hex,
            {"import_id": uuid4().hex, "target_class": "polyimide"}, "same-hash", key,
            {"fingerprint": "test"}, {},
            [{"chunk_id": "c0", "phase": 0, "kind": "classify", "payload": {"smiles": []}}], owner_user_id=current_owner_id())


def test_online_history_owner_unique_clear_and_rls(isolated_assets_db):
    _dsn, connect, _repository = isolated_assets_db
    for owner in (A, B):
        with user_context(owner), connect() as connection:
            history.save_online_history_postgres(connection, owner_user_id=current_owner_id(), material="same", mode="synthesis", max_papers=1,
                                                  result_data={"owner_marker": owner.user_id})
    with user_context(A), connect() as connection:
        # This deliberately unfiltered SQL still cannot read B under the real API role.
        assert connection.execute("SELECT count(*) AS n FROM online_knowledge.history").fetchone()["n"] == 1
        history.clear_online_history_postgres(connection, owner_user_id=current_owner_id())
    with user_context(B), connect() as connection:
        rows = history.list_online_history_postgres(connection, owner_user_id=current_owner_id())
        assert len(rows) == 1 and rows[0]["result_data"]["owner_marker"] == B.user_id


def test_batch_import_job_idempotency_list_and_user_quota(isolated_assets_db):
    _dsn, _connect, repository = isolated_assets_db
    import_id = uuid4().hex
    with user_context(A):
        repository.create_import(import_id, {"a": {"filename": "private.csv", "size_bytes": 100}}, owner_user_id=current_owner_id())
    with user_context(B), pytest.raises(BatchError) as denied:
        repository.get_import(import_id, owner_user_id=current_owner_id())
    assert denied.value.status == 404
    a = _submit(repository, A)
    assert _submit(repository, A)["id"] == a["id"]
    b = _submit(repository, B)
    assert b["id"] != a["id"]
    with pytest.raises(BatchError) as full:
        _submit(repository, A, "another-key")
    assert full.value.code == "user_capacity"
    with user_context(B):
        rows, total = repository.list_jobs(offset=0, limit=20, owner_user_id=current_owner_id())
        assert total == 1 and rows[0]["id"] == b["id"]
        with pytest.raises(BatchError) as denied:
            repository.cancel(a["id"], owner_user_id=current_owner_id())
        assert denied.value.status == 404


def test_batch_disable_stops_unstarted_but_preserves_running_job(isolated_assets_db):
    dsn, _connect, repository = isolated_assets_db
    a = _submit(repository, A)
    with psycopg.connect(dsn) as administrator:
        administrator.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s", (A.user_id,))
    with service_context():
        assert repository.claim("worker", "test") is None
        assert repository.get_job_for_service(a["id"])["status"] == "cancelled"

    b = _submit(repository, B)
    with service_context():
        job, _chunk = repository.claim("worker", "test")
        assert job["id"] == b["id"] and job["start_authorized_at"] is not None
        repository.release_execution(job, 0)
    with psycopg.connect(dsn) as administrator:
        administrator.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s", (B.user_id,))
    with service_context():
        resumed, _chunk = repository.claim("worker", "test")
        assert resumed["id"] == b["id"] and resumed["status"] == "running"


def test_batch_atomic_user_limit_and_import_storage_limit(isolated_assets_db):
    _dsn, _connect, repository = isolated_assets_db
    def submit(key):
        try:
            return _submit(repository, A, key)["id"]
        except BatchError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ["one", "two"]))
    assert results.count("user_capacity") == 1
    with user_context(A):
        with pytest.raises(BatchError) as rejected:
            repository.create_import(uuid4().hex, {"a": {"size_bytes": repository.config.user_import_bytes + 1}}, owner_user_id=current_owner_id())
        assert rejected.value.code == "import_quota"
    with user_context(B):
        repository.create_import(uuid4().hex, {"a": {"size_bytes": 100}}, owner_user_id=current_owner_id())


def test_batch_owner_is_mandatory_even_for_service_and_global_access_is_separate(isolated_assets_db):
    _dsn, _connect, repository = isolated_assets_db
    job = _submit(repository, A)
    with service_context():
        with pytest.raises(TypeError, match="owner_user_id"):
            repository.get_job(job["id"])
        with pytest.raises(TypeError, match="owner_user_id"):
            repository.list_jobs()
        # A service login still respects explicit owner SQL on user methods.
        with pytest.raises(BatchError) as denied:
            repository.get_job(job["id"], owner_user_id=B.user_id)
        assert denied.value.status == 404
        assert repository.get_job_for_service(job["id"])["id"] == job["id"]
        assert len(repository.chunks_for_service(job["id"])) == 1
    with user_context(B):
        # Even an incorrectly supplied owner cannot bypass the API login's RLS.
        with pytest.raises(BatchError) as denied:
            repository.get_job(job["id"], owner_user_id=A.user_id)
        assert denied.value.status == 404
        with pytest.raises(RuntimeError, match="service context"):
            repository.get_job_for_service(job["id"])


def test_online_retention_rows_bytes_and_updates_are_atomic_per_owner(isolated_assets_db, monkeypatch):
    _dsn, connect, _repository = isolated_assets_db
    monkeypatch.setenv('PRIVATE_ONLINE_HISTORY_MAX_ROWS', '1')
    monkeypatch.setenv('PRIVATE_ONLINE_HISTORY_MAX_BYTES', '100')
    def save(owner, material, payload):
        with user_context(owner), connect() as connection:
            history.save_online_history_postgres(connection, owner_user_id=current_owner_id(),material=material,mode='synthesis',max_papers=1,result_data=payload)
    save(A, 'first', {'evidence':'keep'})
    save(A, 'first', {'evidence':'updated'})  # Replacement counts only the final value.
    save(B, 'first', {'evidence':'bob'})
    with pytest.raises(history.OnlineKnowledgeQuotaError):
        save(A, 'second', {})
    with pytest.raises(history.OnlineKnowledgeQuotaError):
        save(A, 'first', {'evidence':'x'*200})
    with user_context(A), connect() as connection:
        assert history.list_online_history_postgres(connection, owner_user_id=current_owner_id())[0]['result_data'] == {'evidence':'updated'}
        history.clear_online_history_postgres(connection, owner_user_id=current_owner_id())
    save(A, 'second', {})
    with user_context(B), connect() as connection:
        assert history.list_online_history_postgres(connection, owner_user_id=current_owner_id())[0]['result_data'] == {'evidence':'bob'}


def test_online_job_quota_bounds_failed_rows_and_completed_results(isolated_assets_db, monkeypatch):
    _dsn, connect, _repository = isolated_assets_db
    monkeypatch.setenv('PRIVATE_ONLINE_JOB_MAX_ROWS', '1')
    monkeypatch.setenv('PRIVATE_ONLINE_JOB_MAX_BYTES', '100')
    for owner in (A,B):
        with user_context(owner), service_context(), connect() as connection:
            history.create_online_job_postgres(connection, owner_user_id=current_owner_id(),job_id=owner.user_id,material='same',mode='synthesis',max_papers=1)
            history.mark_online_job_completed_postgres(connection, owner.user_id, {'evidence':'keep'}, owner_user_id=current_owner_id())
    with user_context(A), service_context(), connect() as connection, pytest.raises(history.OnlineKnowledgeQuotaError):
        history.create_online_job_postgres(connection, owner_user_id=current_owner_id(),job_id='another',material='same',mode='synthesis',max_papers=1)
    with user_context(A), connect() as connection, pytest.raises(history.OnlineKnowledgeQuotaError):
        history.mark_online_job_completed_postgres(connection, A.user_id, {'evidence':'x'*200}, owner_user_id=current_owner_id())
    with user_context(A), connect() as connection:
        assert history.get_online_job_postgres(connection,A.user_id, owner_user_id=current_owner_id())['result'] == {'evidence':'keep'}


def test_online_concurrent_history_saves_cannot_overrun_quota(isolated_assets_db, monkeypatch):
    _dsn, connect, _repository = isolated_assets_db
    monkeypatch.setenv('PRIVATE_ONLINE_HISTORY_MAX_ROWS', '1')
    def save(material):
        try:
            with user_context(A), connect() as connection:
                history.save_online_history_postgres(connection, owner_user_id=current_owner_id(),material=material,mode='synthesis',max_papers=1,result_data={})
            return True
        except history.OnlineKnowledgeQuotaError:
            return False
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ('one','two')))
    assert sorted(results) == [False,True]


def test_persistent_submission_rechecks_disable_after_identity_resolution(isolated_assets_db):
    dsn, connect, repository = isolated_assets_db
    from fastapi import HTTPException
    with psycopg.connect(dsn) as administrator:
        administrator.execute("UPDATE auth.users SET status='disabled' WHERE user_id=%s", (A.user_id,))
    with pytest.raises(BatchError) as batch_denied:
        _submit(repository, A)
    assert batch_denied.value.code == 'account_disabled'
    with user_context(A), service_context(), connect() as connection, pytest.raises(HTTPException) as online_denied:
        history.create_online_job_postgres(connection, owner_user_id=current_owner_id(),job_id='disabled',material='test',mode='synthesis',max_papers=1)
    assert online_denied.value.status_code == 403
    with user_context(A), connect() as connection:
        assert connection.execute('SELECT count(*) AS n FROM online_knowledge.jobs').fetchone()['n'] == 0
