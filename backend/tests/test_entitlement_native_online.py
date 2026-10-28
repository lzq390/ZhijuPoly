"""Online native entitlement tests: isolated PG and mocked provider, never external IO."""
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import psycopg
from fastapi import BackgroundTasks
from task_usage import entitlements as core, native_entitlements as ne, entitlement_config
from app.auth.context import service_context
from app.services.online_knowledge import postgres_history_repository as online, search_service as search
from app.routers import online_knowledge as route
from test_entitlement_repository import entitlement_db, usage_db, connection, policy
from test_usage_repository import A, B
from test_online_knowledge_extraction_validation import model_responses, negative, positive


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    monkeypatch.delenv("USAGE_METERING_ENABLED", raising=False)


@pytest.fixture
def configured(entitlement_db, monkeypatch):
    # Each case owns a distinct synthetic principal. Native operations purposely
    # survive deletion and must not consume another test's concurrent budget.
    owner_id = str(uuid4())
    with psycopg.connect(entitlement_db) as conn:
        conn.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES (%s,%s,'!',false)",
                     (owner_id, "native-test-" + owner_id))
    monkeypatch.setitem(globals(), "A", owner_id)
    value = policy(entitlement_db, owner_id)
    monkeypatch.setattr(entitlement_config, "configured_policy", lambda: value)
    return value


def create(dsn, *, owner=None, job_id=None, key=None, running=True, mode="synthesis"):
    owner = owner or A
    job_id, key = job_id or uuid4().hex, key or uuid4().hex
    with connection(dsn, owner, "nexpoly_service") as conn:
        result = online.create_online_job_postgres(conn, owner_user_id=owner, job_id=job_id,
            material="synthetic test", mode=mode, max_papers=2,
            idempotency_key=key, request_fingerprint="a" * 64)
        assert result["created"]
        assert ne.begin_native_dispatch(conn, owner_user_id=owner, module="online_knowledge",
            native_task_id=job_id, executor_id="online_knowledge:" + job_id)["allowed"]
        if running:
            assert ne.claim_native_running(conn, owner_user_id=owner, module="online_knowledge",
                native_task_id=job_id, executor_id="online_knowledge:" + job_id)["allowed"]
            online.mark_online_job_running_postgres(conn, job_id, owner_user_id=owner)
    return job_id, key


def read(dsn, job_id, owner=None):
    owner = owner or A
    with connection(dsn, owner, "nexpoly_service") as conn:
        return (online.get_online_job_postgres(conn, job_id, owner_user_id=owner),
                ne.get_native(conn, owner_user_id=owner, module="online_knowledge", native_task_id=job_id))


def test_same_key_returns_original_before_changed_policy_and_retention(entitlement_db, configured, monkeypatch):
    job_id, key = create(entitlement_db)
    forbidden = Mock(side_effect=AssertionError("idempotent replay must skip current policy/capacity"))
    monkeypatch.setattr(entitlement_config, "configured_policy", forbidden)
    monkeypatch.setattr(online, "_check_storage_quota", forbidden)
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        result = online.create_online_job_postgres(conn, owner_user_id=A, job_id=uuid4().hex,
            material="synthetic test", mode="synthesis", max_papers=2,
            idempotency_key=key, request_fingerprint="a" * 64)
        assert not result["created"] and result["job"]["job_id"] == job_id
        with pytest.raises(core.EntitlementConflict):
            online.create_online_job_postgres(conn, owner_user_id=A, job_id=uuid4().hex,
                material="changed synthetic", mode="synthesis", max_papers=2,
                idempotency_key=key, request_fingerprint="b" * 64)


def test_native_insert_and_reservation_rollback_together(entitlement_db, configured):
    job_id, key = uuid4().hex, uuid4().hex
    with pytest.raises(RuntimeError, match="synthetic admission failure"):
        with connection(entitlement_db, A, "nexpoly_service") as conn:
            online.create_online_job_postgres(conn, owner_user_id=A, job_id=job_id,
                material="synthetic test", mode="synthesis", max_papers=2,
                idempotency_key=key, request_fingerprint="a" * 64)
            raise RuntimeError("synthetic admission failure")
    assert read(entitlement_db, job_id) == (None, None)


@pytest.mark.parametrize("example,success,failed,expected", [
    (False, 2, 0, "succeeded"), (False, 1, 1, "released"),
    (True, 2, 0, "released"), (False, 0, 0, "released"),
])
def test_actual_summary_qualification_and_terminal_winner(entitlement_db, configured, example, success, failed, expected):
    job_id, key = create(entitlement_db)
    result = {"exampleUsed": example, "synthetic": True}
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        assert online.mark_online_job_completed_postgres(conn, job_id, result, owner_user_id=A,
            entitlement_facts={"successful_extractions": success, "failed_extractions": failed})
    native, op = read(entitlement_db, job_id)
    assert native["status"] == "completed" and native["result"] == result
    assert op["state"] == expected
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        online.mark_online_job_failed_postgres(conn, job_id, "late synthetic failure", owner_user_id=A)
        assert not online.mark_online_job_completed_postgres(conn, job_id, {"different": True}, owner_user_id=A)
    assert read(entitlement_db, job_id) == (native, op)
    with connection(entitlement_db, B) as conn:
        assert ne.get_native(conn, owner_user_id=B, module="online_knowledge", native_task_id=job_id) is None


def test_missing_extraction_evidence_holds_unknown_without_invented_zeros(entitlement_db, configured):
    job_id, _ = create(entitlement_db)
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        assert online.mark_online_job_completed_postgres(conn, job_id, {"exampleUsed": False}, owner_user_id=A)
    native, op = read(entitlement_db, job_id)
    assert native["status"] == "completed" and op["state"] == "unknown"
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        assert not ne.claim_native_running(conn, owner_user_id=A, module="online_knowledge",
            native_task_id=job_id, executor_id="online_knowledge:" + job_id)["allowed"]


def test_result_settlement_fault_rolls_back_native_persistence(entitlement_db, configured, monkeypatch):
    job_id, _ = create(entitlement_db)
    monkeypatch.setattr(core, "settle", Mock(side_effect=RuntimeError("synthetic settlement failure")))
    with pytest.raises(RuntimeError, match="synthetic settlement failure"):
        with connection(entitlement_db, A, "nexpoly_service") as conn:
            online.mark_online_job_completed_postgres(conn, job_id, {"exampleUsed": False}, owner_user_id=A,
                entitlement_facts={"successful_extractions": 1, "failed_extractions": 0})
    native, op = read(entitlement_db, job_id)
    assert native["status"] == "running" and native["result"] is None and op["state"] == "running"


def test_native_recovery_uses_only_changed_rows_and_is_idempotent(entitlement_db, configured):
    interrupted, _ = create(entitlement_db)
    completed, _ = create(entitlement_db, owner=B)
    with connection(entitlement_db, B, "nexpoly_service") as conn:
        online.mark_online_job_completed_postgres(conn, completed, {"exampleUsed": False}, owner_user_id=B,
            entitlement_facts={"successful_extractions": 1, "failed_extractions": 0})
    before = read(entitlement_db, completed, B)
    # Isolated database only: invoke the existing single-backend startup recovery;
    # no new running/death inference is introduced by the entitlement adapter.
    with service_context(), connection(entitlement_db, A, "nexpoly_service") as conn:
        assert online.fail_interrupted_online_jobs_postgres(conn) >= 1
    assert read(entitlement_db, interrupted)[1]["state"] == "released"
    assert read(entitlement_db, completed, B) == before
    with service_context(), connection(entitlement_db, A, "nexpoly_service") as conn:
        assert online.fail_interrupted_online_jobs_postgres(conn) == 0


def test_missing_native_row_cannot_settle_or_erase_completed_operation(entitlement_db, configured):
    job_id, _ = create(entitlement_db)
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        conn.execute("DELETE FROM online_knowledge.jobs WHERE job_id=%s", (job_id,))
        assert not online.mark_online_job_completed_postgres(conn, job_id, {"exampleUsed": False}, owner_user_id=A,
            entitlement_facts={"successful_extractions": 1, "failed_extractions": 0})
        online.mark_online_job_failed_postgres(conn, job_id, "missing", owner_user_id=A)
        assert ne.get_native(conn, owner_user_id=A, module="online_knowledge", native_task_id=job_id)["state"] == "running"


def test_route_replay_does_not_schedule_or_acquire_again(monkeypatch):
    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    prior = {"operation_id": "operation-a", "native_task_id": "old-job", "state": "unknown"}
    monkeypatch.setattr(route, "current_identity", lambda: SimpleNamespace(user_id=A))
    monkeypatch.setattr(ne, "lookup_native", Mock(return_value=prior))
    monkeypatch.setattr(route, "get_online_job_postgres", Mock(return_value={"job_id": "old-job", "status": "running"}))
    forbidden = Mock(side_effect=AssertionError("replay must not admit/configure/run"))
    monkeypatch.setattr(route, "resolve_online_model_access", forbidden)
    monkeypatch.setattr(route, "acquire_admission", forbidden)
    settings = SimpleNamespace(structured_data_backend="postgres", app_postgres_dsn="unused")
    request = SimpleNamespace(headers={"Idempotency-Key": "stable-key"}, state=SimpleNamespace(),
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, postgres_connection_factory=lambda _dsn: Conn())))
    body = SimpleNamespace(model_dump=lambda **kwargs: {"material": "synthetic"})
    tasks = BackgroundTasks()
    response = route.create_online_knowledge_job(body, request, tasks)
    assert response.job_id == "old-job" and response.status == "running" and tasks.tasks == []
    assert request.state.usage_operation_id == "operation-a" and request.state.usage_operation_state == "unknown"


def test_search_counts_actual_extractor_outcomes_without_exposing_them_as_science(monkeypatch):
    papers = [{"abstract": "synthetic", "title": "synthetic"}]
    class Searcher:
        DEFAULT_SOURCES = ["synthetic"]
        def __init__(self, **kwargs): pass
        def search_all(self, *args, **kwargs): return papers
        def deduplicate(self, value): return value
        def enrich_batch_with_crossref(self, value): return value
    class Extractor:
        def __init__(self, **kwargs): pass
        def process_papers(self, *args, **kwargs): return [{"error": None}, {"error": "synthetic failure"}]
        def convert_to_rows(self, **kwargs): return []
        def close(self): pass
    monkeypatch.setattr(search, "SimpleLiteratureSearcher", Searcher)
    monkeypatch.setattr(search, "PolymerExtractor", Extractor)
    for name in ("build_property_points", "build_stats", "build_syntheses", "catalyst_table", "condition_summary",
                 "polymer_type_distribution", "property_condition_distribution", "property_name_distribution",
                 "reaction_type_table", "relationship_distribution", "solvent_distribution", "temperature_distribution", "temperature_labels"):
        monkeypatch.setattr(search, name, lambda *args, **kwargs: [])
    facts = {}
    result = search.run_online_knowledge_search(material="synthetic", mode="synthesis", api_key="synthetic-unused",
        base_url="https://invalid.example", model="synthetic", max_papers=2, extraction_delay_seconds=0,
        execution_facts=facts)
    assert facts == {"successful_extractions": 1, "failed_extractions": 1}
    assert result["exampleUsed"] is False
    assert "successful_extractions" not in result  # Private evidence, not a fabricated science metric.


@pytest.mark.parametrize("mode", ["synthesis", "property"])
@pytest.mark.parametrize("scenario,job_status,settlement,rows", [
    ("invalid", "failed", "released", None),
    ("negative", "completed", "succeeded", 0),
    ("positive", "completed", "succeeded", 1),
    ("mixed", "completed", "released", 1),
])
def test_validated_model_results_drive_real_job_history_and_settlement(
    entitlement_db, configured, monkeypatch, model_responses, mode, scenario, job_status, settlement, rows,
):
    from app.auth.context import Identity, user_context
    from app.models import OnlineKnowledgeSearchRequest
    from app.services.ai_client import INVALID_EXTRACTION_RESULT

    outputs = {"invalid": [{}], "negative": [negative(mode)], "positive": [positive(mode)],
               "mixed": [{}, positive(mode)]}[scenario]
    model_responses.responses.extend(outputs)
    papers = [{"title": f"Fixture {index}", "abstract": "Synthetic evidence", "doi": f"fixture-{index}"}
              for index in range(len(outputs))]

    class Searcher:
        DEFAULT_SOURCES = ("fixture",)
        def __init__(self, **kwargs): pass
        def search_all(self, *args, **kwargs): return papers
        def deduplicate(self, values): return values
        def enrich_batch_with_crossref(self, values): return values

    monkeypatch.setattr(search, "SimpleLiteratureSearcher", Searcher)
    monkeypatch.setattr(route, "postgres_connection", lambda dsn: connection(dsn, A, "nexpoly_service"))
    job_id, _ = create(entitlement_db, running=False, mode=mode)
    body = OnlineKnowledgeSearchRequest(material="synthetic test", mode=mode, max_papers=2, extraction_delay_seconds=0)
    access = route.OnlineModelAccess(api_key="fixture", base_url="https://invalid.example/v1", model="fixture", proxy_url="")
    with user_context(Identity(A)):
        route._execute_online_knowledge_job(job_id, entitlement_db, body, access)
    job, operation = read(entitlement_db, job_id)
    assert job["status"] == job_status
    assert operation["state"] == settlement
    with connection(entitlement_db, A) as conn:
        history = online.list_online_history_postgres(conn, owner_user_id=A)
    if rows is None:
        assert job["result"] is None
        assert INVALID_EXTRACTION_RESULT in job["error_message"]
        assert history == []
    else:
        assert len(job["result"]["dataframe"]) == rows
        assert len(history) == 1 and history[0]["result_data"] == job["result"]
    assert len(model_responses.calls) == len(outputs)
    model_responses.clients[0].close.assert_called_once()


@pytest.mark.parametrize("collection_enabled", [True, False])
def test_first_start_tier_gate_is_independent_of_collection(entitlement_db, configured, monkeypatch, collection_enabled):
    from task_usage import _entitlement_store as store
    from app.task_control import authorize_start
    job_id, _ = create(entitlement_db, running=False)
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        store.set_owner_tier(conn, owner_user_id=A, tier="free")
    try:
        if not collection_enabled:
            monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED")
        with connection(entitlement_db, A, "nexpoly_service") as conn:
            assert not authorize_start(conn, owner_user_id=A, table="online_knowledge.jobs", key_column="job_id", key=job_id)
            row = conn.execute("SELECT status,start_authorized_at FROM online_knowledge.jobs WHERE job_id=%s", (job_id,)).fetchone()
            assert row["status"] == "pending" and row["start_authorized_at"] is None
    finally:
        with connection(entitlement_db, A, "nexpoly_service") as conn:
            store.set_owner_tier(conn, owner_user_id=A, tier="member")


def test_common_native_start_replay_keeps_original_grant_after_tier_change(entitlement_db, configured):
    from task_usage import _entitlement_store as store
    from app.task_control import authorize_start
    job_id, _ = create(entitlement_db, running=False)
    with connection(entitlement_db, A, "nexpoly_service") as conn:
        assert authorize_start(conn, owner_user_id=A, table="online_knowledge.jobs", key_column="job_id", key=job_id)
        before = conn.execute("SELECT start_authorized_at FROM online_knowledge.jobs WHERE job_id=%s", (job_id,)).fetchone()["start_authorized_at"]
        store.set_owner_tier(conn, owner_user_id=A, tier="free")
    try:
        with connection(entitlement_db, A, "nexpoly_service") as conn:
            assert authorize_start(conn, owner_user_id=A, table="online_knowledge.jobs", key_column="job_id", key=job_id)
            assert not authorize_start(conn, owner_user_id=B, table="online_knowledge.jobs", key_column="job_id", key=job_id)
            after = conn.execute("SELECT start_authorized_at FROM online_knowledge.jobs WHERE job_id=%s", (job_id,)).fetchone()["start_authorized_at"]
            assert after == before
    finally:
        with connection(entitlement_db, A, "nexpoly_service") as conn:
            store.set_owner_tier(conn, owner_user_id=A, tier="member")
