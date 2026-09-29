"""Run only against a disposable PostgreSQL instance, using real API-role RLS."""
from uuid import uuid4

import psycopg
import pytest

from app.auth.context import Identity
from app.auth.settings import AuthSettings
from app.services.task_events import read_snapshot
from test_monomer_user_isolation_postgres import isolation_database, isolated, A, B


@pytest.mark.parametrize("module", ["md", "dft"])
def test_real_role_owner_isolation_and_meaningful_changes(isolated, module):
    settings = AuthSettings(application_dsn=isolated.api, auth_dsn=isolated.api, service_dsn=isolated.service)
    jobs = {owner: str(uuid4()) for owner in (A, B)}
    with psycopg.connect(isolated.admin) as connection:
        for owner, job in jobs.items():
            if module == "md":
                connection.execute("""INSERT INTO md.monomer_md_jobs
                    (job_id,owner_user_id,input_smiles,canonical_smiles,status,run_mode,protocol)
                    VALUES (%s,%s,'CCO','CCO','running','formal','Density')""", (job, owner))
            else:
                connection.execute("""INSERT INTO monomer_dft.jobs
                    (job_id,owner_user_id,idempotency_key,request_sha256,request_json,
                     calculation_type,model_name,input_smiles,multiplicity,attempt_token,status)
                    VALUES (%s,%s,%s,%s,'{}','single_point','aimnet2','CCO',1,%s,'running')""",
                    (job, owner, uuid4().hex, 'a' * 64, uuid4().hex * 2))
    def snapshot(owner):
        return read_snapshot(isolated.api, Identity(owner), settings, module)
    before = snapshot(A)
    assert [j['job_id'] for j in before] == [jobs[A]]
    assert [j['job_id'] for j in snapshot(B)] == [jobs[B]]
    table = 'md.monomer_md_jobs' if module == 'md' else 'monomer_dft.jobs'
    with psycopg.connect(isolated.admin) as connection:
        connection.execute(f"UPDATE {table} SET progress_percent=42, updated_at=now() WHERE job_id=%s", (jobs[A],))
    assert snapshot(A) == before, 'Progress and heartbeat must not trigger refresh'
    with psycopg.connect(isolated.admin) as connection:
        connection.execute(f"UPDATE {table} SET status='completed' WHERE job_id=%s", (jobs[A],))
    assert snapshot(A)[0]['status'] == 'completed'
    assert snapshot(B)[0]['status'] == 'running'
    if module == 'dft':
        with psycopg.connect(isolated.admin) as connection:
            connection.execute("UPDATE monomer_dft.jobs SET artifacts_delete_requested_at=now() WHERE job_id=%s", (jobs[A],))
        assert snapshot(A)[0]['artifacts_state'] == 'delete_requested'
        with psycopg.connect(isolated.admin) as connection:
            connection.execute("UPDATE monomer_dft.jobs SET artifacts_deleted_at=now() WHERE job_id=%s", (jobs[A],))
        assert snapshot(A)[0]['artifacts_state'] == 'deleted'
    with psycopg.connect(isolated.admin) as connection:
        connection.execute(f"DELETE FROM {table} WHERE job_id=%s", (jobs[A],))
    assert snapshot(A) == []
    assert len(snapshot(B)) == 1
