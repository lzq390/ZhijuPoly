"""Disposable real-role fixtures for the multiuser acceptance contracts.

Only the database created by test_auth_isolation.auth_database is mutated.
MD uses a controlled double; batch also has real HTTP/preview/export helpers.
Authentication, PostgreSQL roles and RLS are real.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import anyio
from fastapi import FastAPI
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from app.auth.cli import manage_user
from app.auth.context import Identity, user_context, service_context
from app.config import Settings
from app.postgres_database import postgres_connection
from app.routers import monomer_md, monomer_dft, monomer_polymerization_batch, online_knowledge
from app.services.monomer_dft_repository import MonomerDftRepository
from app.services.monomer_md_repository import create_monomer_md_job_postgres
from app.services.online_knowledge import postgres_history_repository as history
from app.services.polymerization_batch.models import BatchSettings, BatchError
from app.services.polymerization_batch.service import BatchService
from test_auth_isolation import auth_database
from test_private_http_support import authenticated_client
from test_monomer_user_isolation_postgres import prepared

PROTOCOLS = ("Density", "HVap", "Compressibility", "Dielectric", "Transport")
OWNER_TABLES = (
    "online_knowledge.history", "online_knowledge.jobs", "md.monomer_md_jobs",
    "monomer_dft.jobs", "polymerization_batch.imports", "polymerization_batch.jobs",
)
PRIVATE_TABLES = (*OWNER_TABLES, "monomer_dft.job_attempts", "monomer_dft.artifacts", "polymerization_batch.chunks")


class ControlledMdWorker:
    def __init__(self):
        self.payloads, self.cancelled, self.deleted = [], [], []

    def get_health(self):
        return {"status": "ok", "mode": "real", "db_configured": True,
                "start_authorization_version": 1, "runtime_ready": True,
                "byteff2_root_exists": True, "active_jobs": 0,
                "protocols": {p: {"protocol": p, "run_mode": "formal", "supported": True,
                                  "runtime_ready": True, "runtime_error": None} for p in PROTOCOLS}}

    def submit_job(self, payload):
        self.payloads.append(payload)
        return SimpleNamespace(worker_id="multiuser-controlled-worker", worker_job_id=payload.job_id, worker_version="test")

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return {"job_id": job_id, "status": "cancel_requested", "message": "accepted"}

    def delete_artifacts(self, job_id):
        self.deleted.append(job_id)
        return {"job_id": job_id, "deleted": True, "artifact_root": "/controlled/" + job_id, "message": "deleted"}


@pytest.fixture(scope="module")
def multiuser_accounts(auth_database):
    # One real password hash can seed multiple disposable users. No deployment
    # account or credential is read or changed.
    with psycopg.connect(auth_database["admin"], row_factory=dict_row) as conn:
        first = manage_user(conn, "create", username="multi" + uuid4().hex, password="initial-test-password")
        encoded = conn.execute("SELECT password_hash FROM auth.users WHERE user_id=%s", (first["user_id"],)).fetchone()["password_hash"]
        users = [first]
        for _ in range(11):
            users.append(conn.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,%s,%s,false) "
                                      "RETURNING user_id,username,status,must_change_password",
                                      (uuid4(), "multi" + uuid4().hex, encoded)).fetchone())
    return users


@pytest.fixture
def multiuser_case(auth_database, multiuser_accounts, monkeypatch, tmp_path):
    with psycopg.connect(auth_database["admin"]) as conn:
        conn.execute("TRUNCATE online_knowledge.history,online_knowledge.jobs,md.monomer_md_jobs,"
                     "monomer_dft.jobs,polymerization_batch.imports,polymerization_batch.jobs CASCADE")
        conn.execute("UPDATE auth.users SET status='active' WHERE user_id=ANY(%s)", ([u["user_id"] for u in multiuser_accounts],))
        conn.execute("UPDATE governance.deployment_control SET drain_enabled=false,reason=NULL,release_sha=NULL,activated_by=NULL")
    monkeypatch.setenv("APP_SERVICE_POSTGRES_DSN", auth_database["service"])
    settings = Settings(app_postgres_dsn=auth_database["api"], allowed_origins="http://testserver",
                        model_enabled=False, monomer_md_submit_enabled=True,
                        monomer_md_worker_base_url="http://controlled.invalid", monomer_md_max_active_jobs=3,
                        monomer_md_rate_limit_per_ip_per_minute=1000, monomer_dft_submit_enabled=False)
    app = FastAPI()
    app.state.settings = settings
    app.state.postgres_connection_factory = postgres_connection
    app.state.monomer_md_worker_client = ControlledMdWorker()
    app.state.monomer_dft_repository = MonomerDftRepository(auth_database["api"])
    app.state.monomer_dft_reconciler = SimpleNamespace(kick=lambda: None)
    app.state.polymerization_batch = BatchService(auth_database["api"], BatchSettings(enabled=True, storage_root=tmp_path / "batch"))
    app.state.polymerization_batch_validation_limiter = anyio.CapacityLimiter(2)
    app.add_exception_handler(BatchError, monomer_polymerization_batch.batch_error_handler)
    app.add_exception_handler(monomer_dft.MonomerDftPublicError, monomer_dft.monomer_dft_public_error_handler)
    for routes in (monomer_md, monomer_dft, monomer_polymerization_batch, online_knowledge):
        app.include_router(routes.router)
    clients = [authenticated_client(app, auth_database, user=user) for user in multiuser_accounts[:3]]
    case = SimpleNamespace(database=auth_database, users=multiuser_accounts, clients=clients, app=app,
                           auth_settings=app.state.auth.settings, batch=app.state.polymerization_batch.repository,
                           dft=app.state.monomer_dft_repository, settings=settings)
    try:
        yield case
    finally:
        for client in clients:
            client.close()


def owner(case, index):
    return str(case.users[index]["user_id"])


def identity(case, index):
    return user_context(Identity(owner(case, index)), case.auth_settings)


def submit_batch(case, index, key=None):
    with identity(case, index), service_context(), case.batch.connection() as conn:
        key = key or uuid4().hex
        previous = case.batch.admit(conn, key, "same-request", owner_user_id=owner(case, index))
        if previous:
            return previous
        return case.batch.insert_job(conn, uuid4().hex,
            {"import_id": uuid4().hex, "target_class": "polyimide"}, "same-request", key,
            {"fingerprint": "controlled"}, {},
            [{"chunk_id": "chunk0", "phase": 0, "kind": "classify", "payload": {"smiles": []}}],
            owner_user_id=owner(case, index))


def seed_private_resources(case):
    resources = []
    request = prepared()
    for index in range(3):
        ids = {"md": uuid4().hex, "online": uuid4().hex, "import": uuid4().hex}
        with identity(case, index):
            with service_context(), postgres_connection(case.database["api"]) as conn:
                result = dict(material=case.users[index]["username"], mode="synthesis", query_time_ms=0,
                              totalPapers=0, max_papers=1, exampleUsed=False, stats={})
                history.save_online_history_postgres(conn, owner_user_id=owner(case, index), material="same-material",
                    mode="synthesis", max_papers=1, result_data=result)
                history.create_online_job_postgres(conn, owner_user_id=owner(case, index), job_id=ids["online"],
                    material=case.users[index]["username"], mode="synthesis", max_papers=1)
                create_monomer_md_job_postgres(conn, owner_user_id=owner(case, index), job_id=ids["md"],
                    input_smiles="CCO", canonical_smiles="CCO", requested_steps=1000, run_mode="formal", protocol="Density")
                ids["history"] = conn.execute("SELECT history_id FROM online_knowledge.history WHERE owner_user_id=%s",
                                               (owner(case, index),)).fetchone()["history_id"]
            ids["dft"] = case.dft.create_job(request, owner_user_id=owner(case, index), idempotency_key="same-key", max_active_jobs=9).job["job_id"]
            case.batch.create_import(ids["import"], {"a": {"filename": "private.csv", "size_bytes": 1}}, owner_user_id=owner(case, index))
        ids["batch"] = submit_batch(case, index, "same-key")["id"]
        with psycopg.connect(case.database["admin"]) as conn:
            conn.execute("INSERT INTO monomer_dft.artifacts(job_id,artifact_id,name,relative_location,media_type,size_bytes,sha256,metadata) "
                         "VALUES(%s,'result','result.json','artifacts/result.json','application/json',1,%s,%s)",
                         (ids["dft"], "0" * 64, Jsonb({"private_marker": owner(case, index)})))
        resources.append(ids)
    return resources


def private_snapshot(case):
    from psycopg import sql
    with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
        return {table: conn.execute(sql.SQL("SELECT to_jsonb(t)::text AS value FROM {} t ORDER BY value").format(
            sql.Identifier(*table.split(".")))).fetchall() for table in PRIVATE_TABLES}


def http_batch_worker(case, *, queue_capacity=8):
    """Publish real readiness without starting the worker computation loop."""
    from app.services.polymerization_batch.chemistry import engine_fingerprint
    from app.services.polymerization_batch.worker import BatchWorker
    service = case.app.state.polymerization_batch
    config = replace(service.config, queue_capacity=queue_capacity)
    service.config = service.repository.config = config
    worker = BatchWorker(case.database["service"], config)
    worker.engine = engine_fingerprint()
    worker.heartbeat(force=True)
    return worker


def http_batch_preview(client):
    """Upload and inspect actual CSV files through the authenticated routes."""
    from app.services.polymerization_batch.service import BASE_PATH
    response = client.post(BASE_PATH + "/imports", files={
        "file_a": ("a.csv", b"id,SMILES\nA1,Nc1ccc(N)cc1\n", "text/csv"),
        "file_b": ("b.csv", b"id,SMILES\nB1,O=C1OC(=O)c2cc3c(cc21)C(=O)OC3=O\n", "text/csv"),
    })
    assert response.status_code == 201, response.text
    uploaded = response.json()
    response = client.post(f"{BASE_PATH}/imports/{uploaded['import_id']}/preview",
                           json={role: uploaded["tables"][role]["mapping"] for role in ("a", "b")})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["can_submit"] is True
    assert preview["statistics"]["valid_pairs"] == 1
    return {"import_id": preview["import_id"], "preview_revision": preview["preview_revision"],
            "target_class": "polyimide"}


def http_batch_submit(client, request, key):
    from app.services.polymerization_batch.service import BASE_PATH
    return client.post(BASE_PATH + "/jobs", headers={"Idempotency-Key": key}, json=request)
