"""Multiuser combinations against real authentication/PostgreSQL roles.

Run with ISOLATION_TEST_ADMIN_DSN pointing at a disposable PostgreSQL 16 server.
NEXPOLY_MULTIUSER_RACE_ROUNDS defaults to 100. These tests do not claim real
scientific/GPU execution: MD uses a controlled worker; batch only runs real
upload/preview and cancelled-job export, never classification/generation.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import re
from threading import Barrier, Event
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from app.auth.cli import manage_user
from app.auth.context import Identity, current_owner_id, service_context, user_context
from app.auth.policy import route_policy
from app.postgres_database import postgres_connection
from app.routers.monomer_md import _create_pending_job_with_capacity_guard
from app.services.monomer_dft_repository import MonomerDftCapacityError, MonomerDftStaleAttempt
from app.services.monomer_md_protocols import DEFAULT_FORMAL_CONFIGS
from app.services.monomer_md_repository import mark_monomer_md_job_completed_postgres
from app.services.polymerization_batch.models import BatchError
from app.services.private_execution import submit_private_job
from app.task_control import acquire_admission, authorize_start
from test_multiuser_support import (
    auth_database, multiuser_accounts, multiuser_case, PROTOCOLS, PRIVATE_TABLES, OWNER_TABLES,
    owner, identity, submit_batch, seed_private_resources, private_snapshot, prepared,
    http_batch_worker, http_batch_preview, http_batch_submit,
)
from test_auth_isolation import login
from test_private_http_support import authenticated_client

RACE_ROUNDS = int(os.environ.get("NEXPOLY_MULTIUSER_RACE_ROUNDS", "100"))
if RACE_ROUNDS < 1:
    raise ValueError("NEXPOLY_MULTIUSER_RACE_ROUNDS must be positive")


@pytest.mark.parametrize("relation", [
    "pi.polymers", "pi.tg_predictions", "pi.monomer_iupac",
])
@pytest.mark.parametrize("role", ["api", "service"])
def test_multiuser_api_role_can_read_shared_reverse_design_objects(auth_database, relation, role):
    """Actual query dependencies remain readable without granting public views."""
    with psycopg.connect(auth_database[role]) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier(*relation.split("."))))
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert not connection.execute("SELECT has_table_privilege(current_user,%s,%s)", (relation, privilege)).fetchone()[0]


def test_multiuser_all_runtime_routes_reject_guest_initial_password_and_operations(multiuser_case, monkeypatch):
    """Use actual registered methods, never business handlers or fake auth."""
    from app.main import create_app
    from app.config import Settings
    import app.task_control as task_control
    case = multiuser_case
    monkeypatch.setenv("AUTH_POSTGRES_DSN", case.database["auth"])
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("AUTH_COOKIE_NAME", "nexpoly_test_session")
    monkeypatch.setattr(task_control, "_start_checker", task_control._start_checker)
    app = create_app(Settings(app_postgres_dsn=case.database["api"], allowed_origins="http://testserver",
                              model_enabled=False, gen_model_enabled=False, retro_model_enabled=False,
                              gpu_preload_mode="lazy", gpu_broker_enabled=False, dev_gpu_operator_enabled=True,
                              dev_gpu_operator_frontend_port=9001))
    guest = TestClient(app)
    ordinary = authenticated_client(app, case.database, user=case.users[0])
    initial = TestClient(app)
    with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
        new_user = manage_user(conn, "create", username="initial" + uuid4().hex, password="initial-test-password")
    login(initial, new_user)
    invalid_sessions = {}
    for state in ("disabled", "expired", "revoked"):
        browser = authenticated_client(app, case.database)
        with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
            if state == "disabled":
                manage_user(conn, "disable", user_id=browser.test_owner_id)
            elif state == "expired":
                conn.execute("UPDATE auth.sessions SET expires_at=now()-interval '1 second' WHERE user_id=%s",
                             (browser.test_owner_id,))
            else:
                conn.execute("UPDATE auth.sessions SET revoked_at=now() WHERE user_id=%s",
                             (browser.test_owner_id,))
        invalid_sessions[state] = browser
    manifest = json.loads((Path(__file__).resolve().parents[2] / "contracts/multiuser_routes.json").read_text())
    reviewed = {(row["method"], row["path"]): row["permission"] for row in manifest["routes"]}
    runtime = {(method, route.path) for route in app.routes for method in getattr(route, "methods", ())}
    assert runtime == set(reviewed), "Runtime routes must match the reviewed independent inventory"
    checked, operations, internal = set(), set(), set()
    substitutions = {"job_id": str(uuid4()), "import_id": uuid4().hex, "history_id": "99999999",
                     "role": "a", "format": "csv", "mol_id": "1", "polymer_id": "1"}
    try:
        for route in app.routes:
            for method in sorted(getattr(route, "methods", ())):
                if method == "OPTIONS":
                    continue
                path = re.sub(r"\{([^}:]+)(?::[^}]+)?\}", lambda match: substitutions.get(match[1], "missing"), route.path)
                expected = reviewed[method, route.path]
                variants = [(path, expected)] if expected != "mixed-template" else [
                    (path, "public"), (path.rsplit("/", 1)[0] + "/x.txt", "member"),
                ]
                for path, policy in variants:
                    # Expected policy comes from the independently reviewed
                    # manifest, never from the implementation under test.
                    assert route_policy(path, method) == policy, (method, path, policy)
                    if policy == "public" or path.startswith("/api/v1/auth/"):
                        continue
                    options = {"content": b"invalid-json", "headers": {"Origin": "http://testserver", "Content-Type": "application/json"}}
                    if policy == "internal":
                        assert guest.request(method, path, **options).status_code == 403, (method, path)
                        for state, browser in invalid_sessions.items():
                            assert browser.request(method, path, **options).status_code == 403, (state, method, path)
                        internal.add((method, route.path))
                        continue
                    assert guest.request(method, path, **options).status_code == 401, (method, path)
                    for state, browser in invalid_sessions.items():
                        response = browser.request(method, path, **options)
                        assert response.status_code == 401, (state, method, path)
                        if method != "HEAD":
                            assert response.json()["code"] == "authentication_required", (state, method, path)
                    response = initial.request(method, path, **options)
                    assert response.status_code == 403, (method, path, response.text)
                    if method != "HEAD":
                        assert response.json()["code"] == ("operation_forbidden" if policy == "operations" else "password_change_required")
                    if policy == "operations":
                        assert ordinary.request(method, path, **options).status_code == 403
                        operations.add((method, route.path))
                    checked.add((method, route.path))
        expected_private = {key for key, permission in reviewed.items() if permission in {"member", "operations", "mixed-template"}
                            and not key[1].startswith("/api/v1/auth/") and key[0] != "OPTIONS"}
        assert checked == expected_private
        assert ("GET", "/api/v1/lab-data/test-projects") in operations
        assert ("GET", "/api/v1/dev-gpu-session/status") in operations
        assert any("authorize-start" in path for _, path in internal)
        for role in ("a", "b"):
            for suffix in ("csv", "xlsx"):
                assert guest.get(f"/api/v1/monomer-polymerization/batch/templates/{role}.{suffix}").status_code == 200
        assert guest.get("/api/v1/auth/session").json()["authenticated"] is False
    finally:
        guest.close()
        initial.close()
        ordinary.close()
        for browser in invalid_sessions.values():
            browser.close()


@pytest.mark.parametrize("table", PRIVATE_TABLES)
def test_multiuser_nine_table_rls_read_write_and_missing_context(multiuser_case, table):
    case = multiuser_case
    seed_private_resources(case)
    relation = sql.Identifier(*table.split("."))
    key = "history_id" if table == "online_knowledge.history" else "id" if table.startswith("polymerization_batch.") and table != "polymerization_batch.chunks" else "job_id"
    column = sql.Identifier(key)
    with identity(case, 1), postgres_connection(case.database["api"]) as conn:
        foreign = conn.execute(sql.SQL("SELECT to_jsonb(t) AS row FROM {} t").format(relation)).fetchone()["row"]
    with identity(case, 0), postgres_connection(case.database["api"]) as conn:
        rows = conn.execute(sql.SQL("SELECT * FROM {}").format(relation)).fetchall()
        assert len(rows) == 1, table
        assert str(rows[0][key]) != str(foreign[key]), table
        assert conn.execute(sql.SQL("UPDATE {} SET {}={} WHERE {}=%s").format(relation, column, column, column), (foreign[key],)).rowcount == 0
        assert conn.execute(sql.SQL("DELETE FROM {} WHERE {}=%s").format(relation, column), (foreign[key],)).rowcount == 0
        # WITH CHECK must reject writing another owner's complete, valid record.
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(sql.SQL("INSERT INTO {} OVERRIDING SYSTEM VALUE SELECT (jsonb_populate_record(NULL::{},%s)).*").format(relation, relation), (Jsonb(foreign),))
        if table in OWNER_TABLES:
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(sql.SQL("UPDATE {} SET owner_user_id=%s").format(relation), (owner(case, 1),))
        assert conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(relation)).fetchone()["n"] == 1
    with postgres_connection(case.database["api"]) as conn:
        assert conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(relation)).fetchone()["n"] == 0


def test_multiuser_http_private_resource_matrix_hides_foreign_ids_without_mutation(multiuser_case):
    case = multiuser_case
    resources = seed_private_resources(case)
    before = private_snapshot(case)
    for actor, target in ((0, 1), (1, 2), (2, 0)):
        client, ids = case.clients[actor], resources[target]
        matrix = [
            ("GET", f"/online-knowledge/jobs/{ids['online']}", None),
            ("DELETE", f"/online-knowledge/history/{ids['history']}", None),
            ("GET", f"/monomer-md/jobs/{ids['md']}", None),
            ("GET", f"/monomer-md/jobs/{ids['md']}/visualization/stages/npt/trajectory", None),
            ("POST", f"/monomer-md/jobs/{ids['md']}/cancel", None),
            ("DELETE", f"/monomer-md/jobs/{ids['md']}/artifacts", None),
            ("DELETE", f"/monomer-md/jobs/{ids['md']}", None),
            ("GET", f"/monomer-dft/jobs/{ids['dft']}", None),
            ("POST", f"/monomer-dft/jobs/{ids['dft']}/cancel", None),
            ("GET", f"/monomer-dft/jobs/{ids['dft']}/artifacts/result", None),
            ("GET", f"/monomer-dft/jobs/{ids['dft']}/bundle", None),
            ("DELETE", f"/monomer-dft/jobs/{ids['dft']}/artifacts", None),
            ("DELETE", f"/monomer-dft/jobs/{ids['dft']}", None),
            ("POST", f"/monomer-polymerization/batch/imports/{ids['import']}/preview", {}),
            ("GET", f"/monomer-polymerization/batch/jobs/{ids['batch']}", None),
            ("GET", f"/monomer-polymerization/batch/jobs/{ids['batch']}/results", None),
            ("POST", f"/monomer-polymerization/batch/jobs/{ids['batch']}/cancel", None),
            ("GET", f"/monomer-polymerization/batch/jobs/{ids['batch']}/artifacts/results.zip", None),
        ]
        for method, path, body in matrix:
            response = client.request(method, "/api/v1" + path, **({"json": body} if body is not None else {}))
            assert response.status_code == 404, (actor, method, path, response.text)
            assert response.headers["cache-control"] == "private, no-store"
            assert case.users[target]["username"] not in response.text
            replacements = {str(value): "999999999" if kind == "history" else str(uuid4()) if kind == "dft" else uuid4().hex
                            for kind, value in ids.items()}
            absent_path = "/".join(replacements.get(segment, segment) for segment in path.split("/"))
            absent = client.request(method, "/api/v1" + absent_path, **({"json": body} if body is not None else {}))
            assert (absent.status_code, absent.json()) == (response.status_code, response.json()), (method, path)
        for domain, field in (("monomer-md", "md"), ("monomer-dft", "dft"), ("monomer-polymerization/batch", "batch")):
            response = client.get(f"/api/v1/{domain}/jobs/{resources[actor][field]}")
            assert response.status_code == 200, response.text
    assert private_snapshot(case) == before
    assert case.app.state.monomer_md_worker_client.cancelled == []
    assert case.app.state.monomer_md_worker_client.deleted == []


def test_multiuser_http_clear_history_and_pagination_leave_other_owners_unchanged(multiuser_case):
    case = multiuser_case
    resources = seed_private_resources(case)
    for index, client in enumerate(case.clients):
        response = client.get("/api/v1/monomer-polymerization/batch/jobs?offset=0&limit=1")
        assert response.status_code == 200, response.text
        assert response.json()["total"] == 1
        assert response.json()["items"][0]["job_id"] == resources[index]["batch"]
        assert response.json()["next_offset"] is None
        assert client.get("/api/v1/monomer-polymerization/batch/jobs?offset=1&limit=1").json()["items"] == []
    for actor in range(3):
        assert case.clients[actor].post("/api/v1/online-knowledge/history/clear").status_code == 200
        for index, client in enumerate(case.clients):
            response = client.get("/api/v1/online-knowledge/history")
            assert response.status_code == 200, response.text
            assert len(response.json()["history"]) == (0 if index <= actor else 1)


def test_multiuser_batch_http_ten_users_compete_for_nine_slots_and_reenter_after_cleanup(multiuser_case):
    """L2 real HTTP/PG/files: queue_capacity=8 allows nine total active jobs."""
    from app.services.polymerization_batch.service import BASE_PATH
    case = multiuser_case
    worker = http_batch_worker(case, queue_capacity=8)
    with ExitStack() as stack:
        clients = list(case.clients)
        for user in case.users[3:10]:
            client = authenticated_client(case.app, case.database, user=user)
            stack.callback(client.close)
            clients.append(client)
        assert len({client.headers["X-Session-Context"] for client in clients}) == 10
        assert len({client.cookies.get(case.auth_settings.cookie_name) for client in clients}) == 10
        requests = [http_batch_preview(client) for client in clients]
        assert len({request["import_id"] for request in requests}) == 10
        worker.heartbeat(force=True)
        gate = Barrier(10)

        def submit(index):
            gate.wait(timeout=15)
            return index, http_batch_submit(clients[index], requests[index], "same-key-across-ten-users")

        with ThreadPoolExecutor(max_workers=10) as executor:
            responses = list(executor.map(submit, range(10)))
        accepted = {index: response.json()["job_id"] for index, response in responses if response.status_code == 202}
        rejected = [(index, response) for index, response in responses if response.status_code != 202]
        assert len(accepted) == 9, [(index, response.status_code, response.text) for index, response in responses]
        assert len(set(accepted.values())) == 9
        assert len(rejected) == 1
        rejected_index, refusal = rejected[0]
        assert (refusal.status_code, refusal.json()["code"]) == (429, "queue_full")
        for index in accepted:
            second = http_batch_submit(clients[index], requests[index], "new-key-second-job")
            assert (second.status_code, second.json()["code"]) == (429, "user_capacity")
        for index, client in enumerate(clients):
            listing = client.get(BASE_PATH + "/jobs").json()
            assert listing["total"] == (1 if index in accepted else 0)
            assert {item["job_id"] for item in listing["items"]} == ({accepted[index]} if index in accepted else set())

        with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
            jobs = conn.execute("SELECT * FROM polymerization_batch.jobs ORDER BY created_at,id").fetchall()
        assert len(jobs) == 9
        assert all(row["status"] == "queued" and row["execution_token"] is None and row["start_authorized_at"] is None for row in jobs)
        assert {str(row["owner_user_id"]): row["id"] for row in jobs} == {owner(case, index): job_id for index, job_id in accepted.items()}
        # Cancel the actual oldest row so one explicit worker claim cannot
        # accidentally start computation for an unrelated queued owner.
        cancelled_id = jobs[0]["id"]
        cancelled_index = next(index for index, job_id in accepted.items() if job_id == cancelled_id)
        response = clients[cancelled_index].post(f"{BASE_PATH}/jobs/{cancelled_id}/cancel")
        assert response.status_code == 200 and response.json()["status"] == "cancelling"
        still_full = http_batch_submit(clients[rejected_index], requests[rejected_index], "retry-after-cancel")
        assert (still_full.status_code, still_full.json()["code"]) == (429, "queue_full")
        with service_context():
            claimed = worker.repository.claim(worker.worker_id, worker.engine["fingerprint"])
        assert claimed is not None
        job, chunk = claimed
        assert job["id"] == cancelled_id and chunk["kind"] == "export"
        assert job["terminal_intent"] == "cancelled"
        # The real bounded exporter exits, removes scratch, commits the terminal
        # state and releases the execution token. No classify/generate is run.
        worker.execute(job, chunk)
        with service_context():
            final = worker.repository.get_job_for_service(cancelled_id)
            chunks = worker.repository.chunks_for_service(cancelled_id)
        assert final["status"] == "cancelled", (final["status"], final["error_code"], final["message"])
        assert final["execution_token"] is None
        assert all(item["status"] == ("completed" if item["kind"] == "export" else "skipped") for item in chunks)
        assert not (worker.root / "jobs" / cancelled_id / "attempts" / job["execution_token"] / "scratch").exists()
        assert clients[cancelled_index].get(f"{BASE_PATH}/jobs/{cancelled_id}/artifacts/results.zip").status_code == 200
        assert clients[rejected_index].get(f"{BASE_PATH}/jobs/{cancelled_id}/artifacts/results.zip").status_code == 404
        worker.heartbeat(force=True)
        admitted = http_batch_submit(clients[rejected_index], requests[rejected_index], "retry-after-cancel")
        assert admitted.status_code == 202, admitted.text
        assert admitted.json()["job_id"] not in accepted.values()
        with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
            jobs = conn.execute("SELECT id,status,execution_token,start_authorized_at FROM polymerization_batch.jobs").fetchall()
        assert len(jobs) == 10
        assert sum(row["status"] == "queued" for row in jobs) == 9
        assert all(row["execution_token"] is None for row in jobs)
        assert all(row["start_authorized_at"] is None for row in jobs if row["id"] != cancelled_id)
        assert {path.name for path in (worker.root / "jobs").iterdir()} == {row["id"] for row in jobs}


def test_multiuser_batch_http_same_key_same_parameters_across_sessions_is_owner_scoped(multiuser_case):
    """Four concurrent real sessions create two jobs, one per owner."""
    from app.services.polymerization_batch.service import BASE_PATH
    case = multiuser_case
    worker = http_batch_worker(case)
    requests = [http_batch_preview(client) for client in case.clients[:2]]
    with ExitStack() as stack:
        sessions = []
        for index in range(2):
            other = authenticated_client(case.app, case.database, user=case.users[index])
            stack.callback(other.close)
            sessions.extend([(index, case.clients[index]), (index, other)])
        assert len({client.headers["X-Session-Context"] for _, client in sessions}) == 4
        worker.heartbeat(force=True)
        gate = Barrier(4)

        def submit(session):
            index, client = session
            gate.wait(timeout=15)
            return index, http_batch_submit(client, requests[index], "same-key-both-owners")

        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(submit, sessions))
        for _, response in responses:
            assert response.status_code == 202, response.text
        by_owner = [{response.json()["job_id"] for owner_index, response in responses if owner_index == index} for index in range(2)]
        assert len(by_owner[0]) == len(by_owner[1]) == 1
        assert by_owner[0].isdisjoint(by_owner[1])
        # C sends the exact A body/key. It must not retrieve A's idempotent
        # result; C owns neither the import nor any existing job for that key.
        foreign = http_batch_submit(case.clients[2], requests[0], "same-key-both-owners")
        assert (foreign.status_code, foreign.json()["code"]) == (404, "not_found")
        with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
            rows = conn.execute("SELECT id,owner_user_id FROM polymerization_batch.jobs").fetchall()
        assert len(rows) == 2
        assert {str(row["owner_user_id"]): {row["id"]} for row in rows} == {owner(case, index): by_owner[index] for index in range(2)}
        assert {path.name for path in (worker.root / "jobs").iterdir()} == {row["id"] for row in rows}
        for index, client in enumerate(case.clients[:3]):
            listing = client.get(BASE_PATH + "/jobs").json()
            assert listing["total"] == (1 if index < 2 else 0)
            assert {item["job_id"] for item in listing["items"]} == (by_owner[index] if index < 2 else set())


@pytest.mark.parametrize("domain,capacity,people", [("md", 3, 10), ("dft", 9, 10), ("batch", 11, 12)])
def test_multiuser_atomic_persistent_global_capacity_and_replay(multiuser_case, domain, capacity, people):
    case = multiuser_case
    gate = Barrier(people)
    request = prepared()
    def submit(index):
        gate.wait(timeout=15)
        try:
            with identity(case, index):
                if domain == "md":
                    job_id = uuid4().hex
                    _create_pending_job_with_capacity_guard(case.settings, job_id=job_id, input_smiles="CCO",
                        canonical_smiles="CCO", requested_steps=1000, protocol="Density", run_mode="formal")
                    return index, job_id
                if domain == "dft":
                    return index, case.dft.create_job(request, owner_user_id=owner(case, index),
                        idempotency_key="same-key", max_active_jobs=capacity).job["job_id"]
                return index, submit_batch(case, index, "same-key")["id"]
        except (HTTPException, MonomerDftCapacityError, BatchError) as exc:
            if isinstance(exc, HTTPException):
                assert exc.status_code == 429
            elif isinstance(exc, BatchError):
                assert (exc.status, exc.code) == (429, "queue_full")
            return index, None
    with ThreadPoolExecutor(max_workers=people) as executor:
        results = list(executor.map(submit, range(people)))
    accepted = [(index, job) for index, job in results if job is not None]
    assert len(accepted) == capacity
    assert len({job for _, job in accepted}) == capacity
    # Idempotency must be checked before a full queue, and must remain per user.
    if domain in {"dft", "batch"}:
        for index, job_id in accepted:
            with identity(case, index):
                replay = case.dft.create_job(request, owner_user_id=owner(case, index), idempotency_key="same-key", max_active_jobs=capacity).job["job_id"] if domain == "dft" else submit_batch(case, index, "same-key")["id"]
                assert replay == job_id


@pytest.mark.parametrize("domain", ["md", "dft", "batch"])
def test_multiuser_same_owner_twenty_atomic_submissions_do_not_consume_other_owners_slot(multiuser_case, domain):
    case, gate, request = multiuser_case, Barrier(20), prepared()
    def submit(index):
        gate.wait(timeout=15)
        try:
            with identity(case, 0):
                if domain == "md":
                    _create_pending_job_with_capacity_guard(case.settings, job_id=uuid4().hex, input_smiles="CCO", canonical_smiles="CCO", requested_steps=1000, protocol="Density", run_mode="formal")
                elif domain == "dft":
                    case.dft.create_job(request, owner_user_id=owner(case, 0), idempotency_key=f"owner-a-submission-{index}", max_active_jobs=3)
                else:
                    submit_batch(case, 0, str(index))
            return True
        except (HTTPException, MonomerDftCapacityError, BatchError) as exc:
            if isinstance(exc, HTTPException):
                assert exc.status_code == 429
            elif isinstance(exc, BatchError):
                assert (exc.status, exc.code) == (429, "user_capacity")
            return False
    with ThreadPoolExecutor(max_workers=20) as executor:
        assert sum(executor.map(submit, range(20))) == 1
    with identity(case, 1):
        if domain == "md":
            _create_pending_job_with_capacity_guard(case.settings, job_id=uuid4().hex, input_smiles="CCN", canonical_smiles="CCN", requested_steps=1000, protocol="Density", run_mode="formal")
        elif domain == "dft":
            assert case.dft.create_job(request, owner_user_id=owner(case, 1), idempotency_key="owner-b-submission", max_active_jobs=3).created
        else:
            assert submit_batch(case, 1)["owner_user_id"] == case.users[1]["user_id"]


@pytest.mark.parametrize("channel,capacity", [("online", 2), ("ai", 2), ("preflight", 2), ("cpu", 2), ("reverse", 2), ("backend_gpu", 9)])
def test_multiuser_twenty_threads_ten_owners_share_channel_and_release_once(channel, capacity):
    owners = [str(uuid4()) for _ in range(10)]
    start, attempted = Barrier(20), Barrier(20)
    def compete(index):
        lease = None
        with user_context(Identity(owners[index % 10])):
            start.wait(timeout=10)
            try:
                lease = acquire_admission(channel)
                return_code = "accepted"
            except HTTPException as exc:
                assert exc.status_code == 429
                return_code = exc.detail["code"]
            finally:
                attempted.wait(timeout=10)
        return owners[index % 10], lease, return_code
    leases = []
    try:
        with ThreadPoolExecutor(max_workers=20) as executor:
            outcomes = list(executor.map(compete, range(20)))
        leases = [lease for _, lease, _ in outcomes if lease]
        assert len(leases) == capacity
        assert len({lease.owner_user_id for lease in leases}) == capacity
        assert all(code in {"accepted", "user_capacity", "channel_capacity"} for _, _, code in outcomes)
    finally:
        # Concurrent duplicate callbacks must not underflow counters or release
        # another user's permit. Then each owner can reuse the channel.
        with ThreadPoolExecutor(max_workers=20) as executor:
            list(executor.map(lambda lease: lease.release(), leases * 2))
    for value in owners:
        with user_context(Identity(value)), acquire_admission(channel):
            pass


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_multiuser_five_md_protocols_http_owner_payload_and_result_contract(multiuser_case, protocol):
    case = multiuser_case
    jobs = []
    for index, client in enumerate(case.clients):
        config = deepcopy(DEFAULT_FORMAL_CONFIGS[protocol])
        config["temperature"] = 298 + index
        response = client.post("/api/v1/monomer-md/jobs", json={"protocol": protocol, "run_mode": "formal", "config_json": config})
        assert response.status_code == 202, response.text
        jobs.append(response.json()["job_id"])
    assert len(set(jobs)) == 3
    assert [p.config_json["temperature"] for p in case.app.state.monomer_md_worker_client.payloads] == [298, 299, 300]
    with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
        for index, job_id in enumerate(jobs):
            row = conn.execute("SELECT owner_user_id,protocol,config_json FROM md.monomer_md_jobs WHERE job_id=%s", (job_id,)).fetchone()
            assert str(row["owner_user_id"]) == owner(case, index)
            assert row["protocol"] == protocol and row["config_json"]["temperature"] == 298 + index
            mark_monomer_md_job_completed_postgres(conn, job_id=job_id, result_data={"private_marker": owner(case, index), "protocol": protocol})
    for index, client in enumerate(case.clients):
        response = client.get("/api/v1/monomer-md/jobs/" + jobs[index])
        assert response.status_code == 200, response.text
        assert response.json()["result"]["private_marker"] == owner(case, index)
        assert client.get("/api/v1/monomer-md/jobs/" + jobs[(index + 1) % 3]).status_code == 404


def test_multiuser_repeated_disable_queued_execution_and_delayed_cleanup(monkeypatch):
    active, executed = {}, []
    monkeypatch.setattr("app.task_control._start_checker", lambda key: active[key])
    a, b = Identity(str(uuid4())), Identity(str(uuid4()))
    active[a.user_id] = active[b.user_id] = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        for round_number in range(RACE_ROUNDS):
            unblock, entered = Event(), Event()
            def blocker():
                entered.set()
                assert unblock.wait(10)
            blocker_future = executor.submit(blocker)
            assert entered.wait(5)
            active[a.user_id] = True
            disabled = []
            with user_context(a):
                future = submit_private_job(executor, lambda: executed.append(round_number), channel="reverse", on_disabled=lambda: disabled.append(current_owner_id()))
            returned = Event()
            future.add_done_callback(lambda _: returned.set())
            active[a.user_id] = False
            unblock.set()
            blocker_future.result(10)
            future.result(10)
            assert returned.wait(5)
            assert disabled == [a.user_id] and executed == []
            active[a.user_id] = True
            running, finish = Event(), Event()
            def work():
                assert current_owner_id() == a.user_id
                running.set()
                assert finish.wait(10)
            with user_context(a):
                inflight = submit_private_job(executor, work, channel="reverse", on_disabled=lambda: pytest.fail("active owner rejected"))
            returned = Event()
            inflight.add_done_callback(lambda _: returned.set())
            try:
                assert running.wait(5)
                assert not inflight.cancel()
                with user_context(a), pytest.raises(HTTPException) as error:
                    acquire_admission("reverse")
                assert error.value.status_code == 429
                with user_context(b), acquire_admission("reverse"):
                    pass
            finally:
                finish.set()
                inflight.result(10)
                assert returned.wait(5)
            with user_context(a), acquire_admission("reverse"):
                pass


@pytest.mark.parametrize("winner", ["disable", "start"])
def test_multiuser_repeated_postgres_disable_start_commit_order(multiuser_case, winner):
    case = multiuser_case
    # A real row lock fixes the ordering; no sleep or timing-based winner guess.
    with ThreadPoolExecutor(max_workers=1) as executor:
        for _ in range(RACE_ROUNDS):
            job_id, entered = uuid4().hex, Event()
            with psycopg.connect(case.database["admin"]) as conn:
                conn.execute("UPDATE auth.users SET status='active' WHERE user_id=%s", (owner(case, 0),))
                conn.execute("INSERT INTO online_knowledge.jobs(job_id,owner_user_id,status,material,mode,max_papers,progress_stage,progress_message) VALUES(%s,%s,'pending','race','synthesis',1,'pending','waiting')", (job_id, owner(case, 0)))
            def start():
                entered.set()
                with psycopg.connect(case.database["service"], row_factory=dict_row) as conn:
                    return authorize_start(conn, owner_user_id=owner(case, 0), table="online_knowledge.jobs", key_column="job_id", key=job_id)
            def disable():
                entered.set()
                with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
                    manage_user(conn, "disable", user_id=owner(case, 0))
            with psycopg.connect(case.database["admin"], row_factory=dict_row) as first:
                if winner == "disable":
                    manage_user(first, "disable", user_id=owner(case, 0))
                    pending = executor.submit(start)
                else:
                    assert authorize_start(first, owner_user_id=owner(case, 0), table="online_knowledge.jobs", key_column="job_id", key=job_id)
                    pending = executor.submit(disable)
                assert entered.wait(5)
                assert not pending.done()
            outcome = pending.result(10)
            if winner == "disable":
                assert outcome is False
            with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
                row = conn.execute("SELECT owner_user_id,start_authorized_at FROM online_knowledge.jobs WHERE job_id=%s", (job_id,)).fetchone()
                assert str(row["owner_user_id"]) == owner(case, 0)
                assert (row["start_authorized_at"] is not None) == (winner == "start")
                assert conn.execute("SELECT status FROM auth.users WHERE user_id=%s", (owner(case, 1),)).fetchone()["status"] == "active"


def test_multiuser_repeated_dft_disabled_attempt_keeps_quota_until_verified_cleanup(multiuser_case):
    case, request = multiuser_case, prepared()
    for round_number in range(RACE_ROUNDS):
        with identity(case, 0):
            job = case.dft.create_job(request, owner_user_id=owner(case, 0), idempotency_key=f"cleanup-round-{round_number}", max_active_jobs=9).job
        with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
            manage_user(conn, "disable", user_id=owner(case, 0))
        attempt = dict(job_id=job["job_id"], attempt_token=job["_attempt_token"],
                       request_sha256=job["request_sha256"], enqueue_sequence=job["_enqueue_sequence"])
        assert not case.dft.authorize_start(**attempt)
        assert case.dft.get_job_for_service(job["job_id"])["status"] == "cancel_requested"
        with psycopg.connect(case.database["admin"], row_factory=dict_row) as conn:
            manage_user(conn, "enable", user_id=owner(case, 0))
        with identity(case, 0), pytest.raises(MonomerDftCapacityError):
            case.dft.create_job(request, owner_user_id=owner(case, 0), idempotency_key=f"premature-{round_number}", max_active_jobs=9)
        snapshot = {"schema_version": 2, **attempt, "status": "cancelled", "stage": "queued",
                    "progress_percent": 0, "error": None, "timings": {}, "artifacts": []}
        # A stale executor must not release the original task's capacity.
        with pytest.raises(MonomerDftStaleAttempt):
            case.dft.apply_worker_snapshot(job_id=job["job_id"], attempt_token="f" * 64, snapshot={**snapshot, "attempt_token": "f" * 64})
        assert case.dft.get_job_for_service(job["job_id"])["status"] == "cancel_requested"
        case.dft.apply_worker_snapshot(job_id=job["job_id"], attempt_token=job["_attempt_token"], snapshot=snapshot)
        assert case.dft.get_job_for_service(job["job_id"])["status"] == "cancelled"
        # Duplicate cleanup delivery is harmless; next round must be admitted.
        case.dft.apply_worker_snapshot(job_id=job["job_id"], attempt_token=job["_attempt_token"], snapshot=snapshot)
        assert case.clients[1].get("/api/v1/monomer-dft/jobs/" + job["job_id"]).status_code == 404
