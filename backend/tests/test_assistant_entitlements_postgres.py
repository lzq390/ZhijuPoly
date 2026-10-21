"""Real disposable PostgreSQL + actual adapter/repository; all providers are stubs."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Barrier, Event, Lock
from types import SimpleNamespace as NS
from uuid import uuid4
import asyncio
import copy

import psycopg
from psycopg.conninfo import make_conninfo
import pytest
from fastapi import HTTPException

from app.auth.context import Identity, user_context
from app.services.entitlement_repository import EntitlementRepository
from app.services import assistant_entitlements as adapter
from app.services import recording_entitlements as summary_adapter
from app.services.assistant_completion import current_completion_evidence, mark_assistant_result
from app.services.usage_provider import provider_call
from task_usage import entitlements as core
from task_usage.entitlement_policy import example_policy, normalize_spec
from test_entitlement_repository import enabled, entitlement_db, usage_db, connection

SPEC = {"message_count": 1, "input_characters": 12, "image_count": 0, "image_bytes": 0,
        "max_output_tokens": 1400, "provider_attempt_limit": 4}
PAYLOAD = {"request": "synthetic assistant request"}


def install_policy(dsn, owner, *, limit=10):
    policy = example_policy()
    policy["version"] = "assistant-pg-" + uuid4().hex
    pool = normalize_spec(policy, "assistant", SPEC)["pool_id"]
    policy["tiers"]["member"]["pools"][pool] = limit
    with connection(dsn, owner, "nexpoly_service") as active:
        core.install_policy(active, policy)
    return policy, pool


@pytest.fixture
def api(entitlement_db):
    owner = str(uuid4())
    policy, pool = install_policy(entitlement_db, owner)
    # PostgreSQL starts each connection as the actual unprivileged application
    # role. Repository sets owner GUC from the captured server Identity.
    dsn = make_conninfo(entitlement_db, options="-c role=nexpoly_api")
    repo = EntitlementRepository(dsn)
    request = NS(headers={"idempotency-key": "pg-round-" + uuid4().hex}, query_params={},
        app=NS(state=NS(entitlement_repository=repo, entitlement_policy=policy)))
    with user_context(Identity(owner)):
        yield NS(owner=owner, policy=policy, pool=pool, repo=repo, request=request,
                 admin_dsn=entitlement_db, api_dsn=dsn)


def prepare(api):
    return adapter.prepare_operation(api.request, "assistant", PAYLOAD, SPEC)


def terminal_provider(*, finish="stop", phase="answer", on_call=None):
    def create(**kwargs):
        if on_call is not None:
            on_call()
        return iter([{"choices": [{"delta": {"content": "complete synthetic result"}, "finish_reason": finish}]}])
    stream = provider_call(create, stream=True, max_tokens=100, usage_phase=phase)
    list(stream)
    mark_assistant_result(validated=True)


def counts(api):
    summary = api.repo.summary(owner_user_id=api.owner, policy_version=api.policy["version"])
    return summary, next(row for row in summary["pools"] if row["pool_id"] == api.pool)


def test_actual_repository_connections_enforce_owner_and_api_role(api):
    with psycopg.connect(api.api_dsn) as active:
        assert active.execute("SELECT current_user").fetchone()[0] == "nexpoly_api"
    operation = prepare(api)
    assert api.repo.get(owner_user_id=api.owner, operation_id=operation.operation["operation_id"])
    with user_context(Identity(str(uuid4()))):
        assert api.repo.get(owner_user_id=api.owner, operation_id=operation.operation["operation_id"]) is None


def test_result_and_success_rollback_together_then_retry_only_persistence(api, monkeypatch):
    operation = prepare(api); operation.begin()
    calls = []
    with operation.scope(): terminal_provider(on_call=lambda: calls.append(1))
    original = core.settle
    observations = []
    def fail_after_sql(active, **kwargs):
        result = original(active, **kwargs)
        assert result["operation"]["state"] == "succeeded"
        assert result["operation"]["result_json"]["result"] == {"message": "durable result"}
        # A second real connection cannot observe an uncommitted result or debit.
        prior = api.repo.get(owner_user_id=api.owner, operation_id=operation.operation["operation_id"])
        summary, pool = counts(api)
        observations.append((prior["state"], prior["result_json"], pool["success_used"], pool["reserved"]))
        raise RuntimeError("injected transaction rollback")
    monkeypatch.setattr(core, "settle", fail_after_sql)
    with pytest.raises(RuntimeError): operation.complete({"message": "durable result"})
    assert observations == [("running", None, 0, 1)]
    prior = api.repo.get(owner_user_id=api.owner, operation_id=operation.operation["operation_id"])
    assert prior["state"] == "running" and prior["result_json"] is None
    assert counts(api)[1]["success_used"] == 0
    monkeypatch.setattr(core, "settle", original)
    operation.complete({"message": "durable result"})
    saved = api.repo.get(owner_user_id=api.owner, operation_id=operation.operation["operation_id"])
    assert saved["state"] == "succeeded" and saved["qualified"] is True
    assert saved["result_json"]["result"] == {"message": "durable result"}
    assert (counts(api)[1]["success_used"], counts(api)[1]["reserved"]) == (1, 0)
    assert calls == [1]  # The second attempt retried only the known local result.


def test_lost_response_replay_uses_stored_result_before_current_policy_and_no_provider(api):
    operation = prepare(api); calls = []
    def source():
        terminal_provider(on_call=lambda: calls.append(1))
        yield adapter.sse("done", {"message": "persisted before lost response"})
    stream = adapter.wrap_chat(source(), operation)
    original = next(stream)
    # A committed result exists before transport delivery; discard this frame.
    stream.close()
    api.request.app.state.entitlement_policy = None
    api.request.app.state.entitlement_repository = EntitlementRepository(api.api_dsn)
    replay = prepare(api)
    assert replay.operation["operation_id"] == operation.operation["operation_id"]
    frames = list(adapter.wrap_chat(lambda: pytest.fail("replay cannot invoke provider"), replay))
    assert frames == [original] and calls == [1]
    summary, pool = counts(api)
    assert (summary["operation_count"], summary["execution_attempts"], pool["success_used"], pool["reserved"]) == (1, 1, 1, 0)


def test_eight_concurrent_same_key_requests_claim_one_executor_and_call_one_provider(api):
    barrier, entered, finish, losers_done = Barrier(8), Event(), Event(), Event()
    lock, calls, rejected = Lock(), [], []
    def call_provider():
        with lock: calls.append(1)
        entered.set()
        assert finish.wait(10)
    def task(_):
        with user_context(Identity(api.owner)):
            barrier.wait(10)
            try:
                operation = prepare(api)
            except HTTPException as error:
                with lock:
                    rejected.append(error.status_code)
                    if len(rejected) == 7: losers_done.set()
                return "pending"
            def source():
                terminal_provider(on_call=call_provider)
                yield adapter.sse("done", {"message": "one executor"})
            return list(adapter.wrap_chat(source(), operation))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(task, i) for i in range(8)]
        try:
            assert entered.wait(10)
            assert losers_done.wait(10)
        finally:
            finish.set()
        results = [future.result(timeout=10) for future in futures]
    assert rejected == [409] * 7 and calls == [1]
    assert results.count("pending") == 7
    summary, pool = counts(api)
    assert (summary["operation_count"], summary["execution_attempts"], pool["success_used"], pool["reserved"]) == (1, 1, 1, 0)
    rows = api.repo.list_operations(owner_user_id=api.owner, module="assistant")
    assert len(rows) == 1 and rows[0]["state"] == "succeeded" and rows[0]["executor_id"]


def test_provider_timeout_unknown_retains_capacity_and_rejects_free_retry(api):
    api.policy, api.pool = install_policy(api.admin_dsn, api.owner, limit=1)
    api.request.app.state.entitlement_policy = api.policy
    operation = prepare(api); operation.begin(); calls = []
    def create(**_):
        calls.append(1)
        raise TimeoutError("synthetic provider timeout")
    with operation.scope(), pytest.raises(TimeoutError): provider_call(create, max_tokens=100, usage_phase="answer")
    operation.abort()
    summary, pool = counts(api)
    assert (pool["success_used"], pool["reserved"], pool["unknown"]) == (0, 1, 1)
    assert api.repo.get(owner_user_id=api.owner, operation_id=operation.operation["operation_id"])["state"] == "unknown"
    with pytest.raises(HTTPException) as conflict: prepare(api)
    assert conflict.value.status_code == 409
    api.request.headers = {"idempotency-key": "new-round-" + uuid4().hex}
    with pytest.raises(HTTPException) as limit: prepare(api)
    assert limit.value.status_code == 429
    with pytest.raises(core.EntitlementConflict):
        api.repo.release(owner_user_id=api.owner, operation_id=operation.operation["operation_id"],
            generation=operation.operation["generation"], reason="not_dispatched", proof={"kind": "not_dispatched"})
    assert counts(api)[1]["reserved"] == 1 and calls == [1]


def test_clean_eof_does_not_create_a_success_receipt(api):
    operation = prepare(api)
    def source():
        terminal_provider(finish=None)
        yield adapter.sse("done", {"message": "not a proven final"})
    with pytest.raises(HTTPException): next(adapter.wrap_chat(source(), operation))
    got = api.repo.get(owner_user_id=api.owner, operation_id=operation.operation["operation_id"])
    assert got["state"] == "unknown" and got["result_json"] is None and got["qualified"] is None
    assert (counts(api)[1]["success_used"], counts(api)[1]["reserved"]) == (0, 1)


def test_summary_snapshot_result_replay_uses_real_pg_after_memory_eviction(api, monkeypatch):
    record = NS(started_at="synthetic-start", ended_at="synthetic-end", events=[{"event": "view", "value": "frozen"}],
        summary=None, summary_lock=asyncio.Lock())
    store = NS(quotas=NS(summary_max_tokens=4096, summary_output_bytes=20000), settings=NS(),
        get_recording=lambda *_a, **_kw: record, _check_size=lambda _: None)
    api.request.app.state.browsing_recording = store
    monkeypatch.setattr(summary_adapter, "admission", lambda _: nullcontext())
    monkeypatch.setattr(summary_adapter, "authorize_memory_start", lambda _: True)
    calls = []
    async def summary(events, _settings):
        calls.append(copy.deepcopy(events))
        terminal_provider(phase="summary")
        return {"summary": "frozen durable summary", "generated": True}
    monkeypatch.setattr(summary_adapter, "generate_knowledge_summary", summary)
    async def check():
        execution = await summary_adapter.prepare_summary(api.request, "pg-record-1")
        expected_version = execution.version
        result = await execution.json_result()
        saved = api.repo.get(owner_user_id=api.owner, operation_id=execution.operation.operation["operation_id"])
        assert saved["state"] == "succeeded"
        assert saved["result_json"]["metadata"] == {"snapshot_sha256": expected_version}
        assert saved["result_json"]["result"] == result
        def absent(*_a, **_kw): raise HTTPException(404)
        store.get_recording = absent
        api.request.app.state.entitlement_policy = None
        replay = await summary_adapter.prepare_summary(api.request, "pg-record-1")
        assert await replay.json_result() == result
        assert len(calls) == 1 and not record.summary_lock.locked()
    asyncio.run(check())


@pytest.mark.parametrize("quota_enabled", [True, False])
def test_queued_owner_downgraded_to_free_never_starts_provider(api, monkeypatch, quota_enabled):
    from app.auth.settings import AuthSettings
    from app.auth.service import AuthService
    from app.services.private_execution import bounded_stream
    from app.task_control import acquire_admission
    # This is a real synthetic account and real AuthService start check, not an
    # authorization callback stub. No sessions, passwords or login tokens exist.
    with psycopg.connect(api.admin_dsn) as active:
        active.execute("INSERT INTO auth.users(user_id,username,password_hash,must_change_password) VALUES(%s,%s,'!',false)",
                       (api.owner, "assistant-pg-" + uuid4().hex[:12]))
    settings = AuthSettings(application_dsn=api.api_dsn,
        auth_dsn=make_conninfo(api.admin_dsn, options="-c role=nexpoly_auth"),
        service_dsn=make_conninfo(api.admin_dsn, options="-c role=nexpoly_service"))
    calls = []
    with user_context(Identity(api.owner), settings):
        assert AuthService(settings).authorize_memory_start(api.owner) is True
        @bounded_stream("ai")
        def body():
            terminal_provider(on_call=lambda: calls.append(1))
            yield adapter.sse("done", {"message": "must not execute"})
        if not quota_enabled:
            monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "false")
        operation = prepare(api)
        queued = adapter.wrap_chat(body(), operation)
        # Service-side tier changes are synthetic test state only. If quota is
        # off, install the access-tier row through its enabled admin hook first.
        monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
        with connection(api.admin_dsn, api.owner, "nexpoly_service") as active:
            core.set_owner_tier(active, owner_user_id=api.owner, tier="free")
        if not quota_enabled:
            monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "false")
        try:
            with pytest.raises(HTTPException) as denial:
                next(queued)
            assert denial.value.status_code == 403
            assert calls == []
            if quota_enabled:
                saved = api.repo.get(owner_user_id=api.owner, operation_id=operation.operation["operation_id"])
                assert saved["state"] == "released" and saved["qualified"] is False
                assert counts(api)[1]["success_used"] == 0
        finally:
            queued.close()
        with acquire_admission("ai"):
            pass  # The execution permit is returned after the actual denial.
