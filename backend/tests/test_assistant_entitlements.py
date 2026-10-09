"""Logical delivery/quota tests. In-memory repository and fake providers only."""
from __future__ import annotations
import asyncio
import copy
import json
from pathlib import Path
from threading import RLock, Thread
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from fastapi import HTTPException
from app.auth.context import Identity, user_context
from task_usage.entitlements import EntitlementConflict
from app.services import assistant_entitlements as adapter
from app.services.assistant_completion import CompletionEvidence, completion_scope, mark_assistant_result
from app.services.usage_provider import provider_call, close_provider_stream
from app.services import recording_entitlements as summary_adapter

OWNER = "12345678-1234-1234-1234-123456789abc"
OTHER = "23456789-1234-1234-1234-123456789abc"

class Repository:
    def __init__(self):
        self.rows, self.keys, self.calls = {}, {}, []
        self.lock = RLock()
    def lookup(self, *, owner_user_id, module, idempotency_key, request_fingerprint):
        with self.lock:
            key = (owner_user_id, module, idempotency_key)
            if key not in self.keys:
                return None
            row = self.rows[self.keys[key]]
            if row["fingerprint"] != request_fingerprint:
                raise EntitlementConflict()
            return copy.deepcopy(row)
    def reserve(self, *, owner_user_id, module, idempotency_key, request_fingerprint, policy_version, spec):
        with self.lock:
            found = self.lookup(owner_user_id=owner_user_id, module=module,
                idempotency_key=idempotency_key, request_fingerprint=request_fingerprint)
            if found:
                return {"created": False, "operation": found}
            op = {"operation_id": str(uuid4()), "owner_user_id": owner_user_id, "module": module,
                "state": "reserved", "generation": 1, "spec": spec, "policy_version": policy_version,
                "fingerprint": request_fingerprint, "result_json": None}
            self.rows[op["operation_id"]] = op
            self.keys[(owner_user_id, module, idempotency_key)] = op["operation_id"]
            self.calls.append(("reserve", op["operation_id"]))
            return {"created": True, "operation": copy.deepcopy(op)}
    def get(self, *, owner_user_id, operation_id):
        row = self.rows.get(operation_id)
        return copy.deepcopy(row) if row and row["owner_user_id"] == owner_user_id else None
    def transition(self, name, **kw):
        with self.lock:
            row = self.rows[kw["operation_id"]]
            assert row["owner_user_id"] == kw["owner_user_id"]
            assert row["generation"] == kw["generation"]
            self.calls.append((name, row["operation_id"]))
            if name == "begin_dispatch":
                assert row["state"] == "reserved"
                row.update(state="dispatching", generation=row["generation"] + 1, executor_id=kw["executor_id"])
            elif name == "claim_running":
                assert row["state"] == "dispatching" and row["executor_id"] == kw["executor_id"]
                row["state"] = "running"
            elif name == "settle":
                assert row["state"] in {"running", "unknown"}
                row.update(state="succeeded" if kw["qualified"] else "released", result_json=kw["result_json"],
                    qualified=kw["qualified"], outcome=kw["outcome"])
            elif name == "release":
                if row["state"] == "reserved":
                    assert kw["proof"] == {"kind": "not_dispatched"}
                else:
                    assert row["state"] == "dispatching"
                    assert kw["proof"] == {"kind": "dispatch_rejected_before_execution", "executor_id": row["executor_id"]}
                row["state"] = "released"
            else:
                row["state"] = "unknown"
            return {"transitioned": True, "operation": copy.deepcopy(row)}
    def __getattr__(self, name):
        return lambda **kw: self.transition(name, **kw)

@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("USAGE_ENTITLEMENTS_ENABLED", "true")
    policy = json.loads((Path(__file__).resolve().parents[2] / "config/examples/entitlements-example-v1.json").read_text())
    repo = Repository()
    request = NS(headers={"idempotency-key": "round-00001"}, query_params={},
                 app=NS(state=NS(entitlement_repository=repo, entitlement_policy=policy)))
    with user_context(Identity(OWNER)):
        yield request, repo


def operation(request, payload=None):
    return adapter.prepare_operation(request, "assistant", payload or {"text": "hello"},
        {"message_count": 1, "input_characters": 10, "image_count": 0, "image_bytes": 0,
         "max_output_tokens": 1400, "provider_attempt_limit": 4})


def terminal_answer(evidence, *, finish="stop", phase="answer", refusal=False):
    with completion_scope(evidence):
        stream = provider_call(lambda **_: iter([{"choices": [{"finish_reason": finish,
            "delta": {"content": "answer", "refusal": "refused" if refusal else None}}]}]),
            stream=True, max_tokens=100, usage_phase=phase)
        list(stream)
        close_provider_stream(stream)
        mark_assistant_result(validated=True)


def test_feature_off_does_not_require_key_owner_repository_or_policy(monkeypatch):
    monkeypatch.delenv("USAGE_ENTITLEMENTS_ENABLED", raising=False)
    assert adapter.prepare_operation(NS(), "assistant", {}, {}) is None


def test_success_persists_before_done_then_replays_without_policy_or_provider(setup):
    request, repo = setup
    op = operation(request)
    def source():
        terminal_answer(op.evidence)
        yield adapter.sse("done", {"message": "answer"})
    stream = adapter.wrap_chat(source(), op)
    frame = next(stream)
    assert '"answer"' in frame
    assert repo.rows[op.operation["operation_id"]]["state"] == "succeeded"
    stream.close()  # simulated lost response after persistence
    request.app.state.entitlement_policy = None
    replay = operation(request)
    assert replay.operation["operation_id"] == op.operation["operation_id"]
    assert list(replay.replay_frames()) == [frame]
    assert [name for name, _ in repo.calls].count("reserve") == 1
    assert [name for name, _ in repo.calls].count("settle") == 1


def test_replay_is_owner_scoped_and_payload_conflict_cannot_execute(setup):
    request, repo = setup
    op = operation(request)
    with pytest.raises(HTTPException) as error:
        operation(request, {"text": "changed"})
    assert error.value.status_code == 409
    with user_context(Identity(OTHER)):
        own = operation(request)
    assert own.operation["operation_id"] != op.operation["operation_id"]


def test_in_progress_replay_rejects_without_second_reservation(setup):
    request, repo = setup
    op = operation(request)
    with pytest.raises(HTTPException) as error:
        operation(request)
    assert error.value.status_code == 409
    assert error.value.headers == op.headers
    assert len(repo.rows) == 1


def test_unstarted_stream_close_releases_only_not_dispatched(setup):
    request, repo = setup
    op = operation(request)
    executed = []
    def source():
        executed.append(True)
        yield "unused"
    adapter.wrap_chat(source(), op).close()
    assert executed == []
    assert op.operation["state"] == "released"
    assert [name for name, _ in repo.calls] == ["reserve", "release"]


def test_clean_eof_with_done_is_not_a_success_or_free_retry(setup):
    request, repo = setup
    op = operation(request)
    def source():
        terminal_answer(op.evidence, finish=None)
        yield adapter.sse("done", {"message": "partial"})
    with pytest.raises(HTTPException):
        next(adapter.wrap_chat(source(), op))
    assert op.operation["state"] == "unknown"
    assert not any(name == "settle" for name, _ in repo.calls)


def test_length_terminal_without_success_retains_unknown_on_done(setup):
    request, _ = setup
    op = operation(request)
    op.begin()
    terminal_answer(op.evidence, finish="length")
    with pytest.raises(HTTPException):
        op.complete({"message": "truncated"})
    assert op.operation["state"] == "unknown"


def test_network_timeout_retains_capacity_and_never_retries(setup):
    request, repo = setup
    op = operation(request); op.begin()
    calls = []
    def create(**_):
        calls.append(True)
        raise TimeoutError("private-secret")
    with op.scope(), pytest.raises(TimeoutError):
        provider_call(create, max_tokens=20)
    op.abort()
    assert len(calls) == 1 and op.operation["state"] == "unknown"
    assert "private-secret" not in repr(repo.rows)


def test_explicit_http_rejection_can_settle_failed_result_zero(setup):
    request, _ = setup
    op = operation(request); op.begin()
    class Rejected(Exception):
        status_code = 429
    def create(**_): raise Rejected()
    with op.scope(), pytest.raises(Rejected):
        provider_call(create, max_tokens=20)
    op.abort()
    assert op.operation["state"] == "released"
    assert op.operation["outcome"] == "failed"
    assert op.replay["error"]["code"] == "assistant_failed"


@pytest.mark.parametrize("outcome", ["clarification", "no_events", "cache_hit"])
def test_known_free_business_terminal_is_durable(setup, outcome):
    request, _ = setup
    op = operation(request); op.begin()
    with op.scope(): mark_assistant_result(outcome, validated=True, local_result=True)
    op.complete({"message": "known"})
    assert op.operation["state"] == "released" and op.operation["outcome"] == outcome
    assert operation(request).replay == op.replay


def test_refusal_is_free_only_with_provider_terminal(setup):
    request, _ = setup
    op = operation(request); op.begin()
    terminal_answer(op.evidence, refusal=True)
    op.complete({"message": "cannot answer"})
    assert op.operation["outcome"] == "refusal" and not op.operation["qualified"]


def test_tool_result_survives_summary_failure_and_transport_close(setup):
    request, repo = setup
    op = operation(request)
    def source():
        terminal_answer(op.evidence, phase="intent")
        with op.scope(): mark_assistant_result(validated=True, local_result=True)
        yield adapter.sse("skill_result", {"skill_name": "fake", "result": {"value": 3}})
        raise TimeoutError()
    stream = adapter.wrap_chat(source(), op)
    next(stream)
    stream.close()
    assert op.operation["qualified"] is True
    assert op.replay["events"][0]["event"] == "skill_result"
    assert [name for name, _ in repo.calls].count("settle") == 1


def test_fallback_attempts_are_children_of_one_delivery(setup):
    request, repo = setup
    op = operation(request); op.begin()
    with op.scope():
        provider_call(lambda **_: {"status": "incomplete", "output": []}, usage_api="responses",
                      usage_phase="answer", max_output_tokens=100)
    terminal_answer(op.evidence)
    op.complete({"message": "fallback answer"})
    assert op.evidence.attempts == 2 and op.operation["qualified"] is True
    assert [name for name, _ in repo.calls].count("settle") == 1


def test_attempt_and_output_limits_reject_before_an_extra_provider_call():
    evidence = CompletionEvidence(1, 100)
    calls = []
    with completion_scope(evidence):
        with pytest.raises(HTTPException):
            provider_call(lambda **_: calls.append(1), max_tokens=101)
        provider_call(lambda **_: calls.append(1), max_tokens=100)
        with pytest.raises(HTTPException):
            provider_call(lambda **_: calls.append(1), max_tokens=100)
    assert calls == [1]


def test_tg_producer_persists_even_when_consumer_disconnected(setup):
    request, _ = setup
    op = operation(request)
    def source():
        terminal_answer(op.evidence)
        yield NS(event="done", payload={"message": "complete"})
    def worker():
        stream = adapter.wrap_chat(source(), op)
        try:
            next(stream)  # persist before route checks its cancelled flag
        finally:
            stream.close()
    thread = Thread(target=worker)
    thread.start(); thread.join(2)
    assert not thread.is_alive() and op.operation["state"] == "succeeded"


def test_http_consumer_close_does_not_release_running_tg_producer(setup):
    request, repo = setup
    op = operation(request); op.begin()
    wrapper = adapter.wrap_chat(iter(()), op, producer=False)
    wrapper.close()
    assert op.operation["state"] == "running"
    assert not any(name in {"release", "settle", "mark_unknown"} for name, _ in repo.calls)
    op.abort()


def test_false_claim_never_executes_source(setup, monkeypatch):
    request, repo = setup
    op = operation(request); executed = []
    monkeypatch.setattr(repo, "claim_running", lambda **_: {"transitioned": False, "operation": op.operation})
    def source():
        executed.append(True)
        yield "x"
    with pytest.raises(HTTPException): next(adapter.wrap_chat(source(), op))
    assert executed == [] and op.operation["state"] == "unknown"


def test_small_frozen_result_limit_fails_closed(setup):
    request, _ = setup
    op = operation(request); op.begin(); op.policy = {**op.policy, "result_max_bytes": 128}
    terminal_answer(op.evidence)
    with pytest.raises((HTTPException, ValueError)):
        op.complete({"message": "x" * 500})
    op.abort()
    assert op.operation["state"] != "succeeded"


def summary_store(request):
    record = NS(started_at="start", ended_at="end", events=[{"event": "view", "value": "old"}],
                summary=None, summary_lock=asyncio.Lock())
    store = NS(quotas=NS(summary_max_tokens=4096, summary_output_bytes=20000), settings=NS(),
               get_recording=lambda *_a, **_kw: record, _check_size=lambda _: None)
    request.app.state.browsing_recording = store
    return store, record


def test_summary_unstarted_close_returns_lock_and_reservation(setup):
    request, _ = setup
    _, record = summary_store(request)
    async def check():
        execution = await summary_adapter.prepare_summary(request, "record-1")
        assert record.summary_lock.locked()
        await execution.aclose()
        assert not record.summary_lock.locked()
        assert execution.operation.operation["state"] == "released"
    asyncio.run(check())


def test_summary_snapshot_success_replay_survives_source_loss(setup, monkeypatch):
    request, repo = setup
    store, record = summary_store(request)
    monkeypatch.setattr(summary_adapter, "admission", lambda _: __import__('contextlib').nullcontext())
    monkeypatch.setattr(summary_adapter, "authorize_memory_start", lambda _: True)
    calls = []
    async def generate(events, _settings):
        calls.append(copy.deepcopy(events))
        from app.services.assistant_completion import current_completion_evidence
        terminal_answer(current_completion_evidence(), phase="summary")
        return {"summary": "finished", "generated": True}
    monkeypatch.setattr(summary_adapter, "generate_knowledge_summary", generate)
    async def check():
        execution = await summary_adapter.prepare_summary(request, "record-1")
        expected = execution.version
        record.events[0]["value"] = "changed-after-freeze"
        result = await execution.json_result()
        assert result["frozen_version"] == expected
        assert calls[0][0]["value"] == "old"
        store.get_recording = lambda *_a, **_kw: (_ for _ in ()).throw(HTTPException(404))
        request.app.state.entitlement_policy = None
        replay = await summary_adapter.prepare_summary(request, "record-1")
        assert await replay.json_result() == result
        assert len(calls) == 1 and len(repo.rows) == 1
    asyncio.run(check())


def test_summary_wrong_frozen_version_never_reserves(setup):
    request, repo = setup
    _, record = summary_store(request)
    request.query_params = {"frozen_version": "not-this-version"}
    with pytest.raises(HTTPException): asyncio.run(summary_adapter.prepare_summary(request, "record-1"))
    assert not repo.rows and not record.summary_lock.locked()


def chat_body():
    return NS(messages=[NS(role="user", content="hello")], context=NS(modules=[], active_module=None),
              page_context=None, model_dump=lambda **_: {"messages": [{"role": "user", "content": "hello"}]},
              model_dump_json=lambda: '{"messages": [{"role": "user", "content": "hello"}]}')


def test_image_source_is_part_of_request_identity(setup):
    request, _ = setup
    body = chat_body()
    adapter.prepare_chat(request, body, "tg_assistant", images=[(b"x", "image/png")], image_sources=["canvas"])
    with pytest.raises(HTTPException) as error:
        adapter.prepare_chat(request, body, "tg_assistant", images=[(b"x", "image/png")], image_sources=["user"])
    assert error.value.status_code == 409


def test_lazy_admission_is_not_acquired_for_unstarted_image_request(setup):
    request, _ = setup
    op = operation(request); calls = []
    def factory():
        calls.append(True)
        return iter(())
    adapter.wrap_chat(factory, op).close()
    assert calls == [] and op.operation["state"] == "released"


def test_responses_complete_is_required_and_finish_flags_not_inferred():
    evidence = CompletionEvidence(2, 100)
    with completion_scope(evidence):
        stream = provider_call(lambda **_: iter([{"type": "response.completed", "response": {"output": []}}]),
                               max_output_tokens=100, stream=True, usage_api="responses", usage_phase="answer")
        list(stream)
        mark_assistant_result(validated=True)
        assert evidence.facts(True)["completion_proven"] is True
    unknown = CompletionEvidence(1, 100)
    with completion_scope(unknown):
        stream = provider_call(lambda **_: iter([{"type": "response.output_text.delta", "delta": "text"}]),
                               max_output_tokens=100, stream=True, usage_api="responses", usage_phase="answer")
        list(stream)
        mark_assistant_result(validated=True)
        assert unknown.facts(True)["completion_proven"] is False


def test_actual_plain_route_header_and_replay_no_service_call(setup, monkeypatch):
    from app.routers import assistant as route
    from app.services.assistant_completion import current_completion_evidence
    request, repo = setup
    request.app.state.settings = NS(structured_data_backend="postgres", assistant_api_key="fake",
        assistant_base_url="https://unused.invalid", assistant_model="fake", model_enabled=False,
        model_dir_path=Path("/unused"), ai_proxy_url="")
    calls = []
    def service(**_):
        calls.append(True)
        terminal_answer(current_completion_evidence())
        yield NS(event="done", payload={"message": "route completed"})
    monkeypatch.setattr(route, "stream_assistant_events", service)
    response = route.stream_chat(chat_body(), request)
    frame = next(response.private_content)
    response.private_content.close()
    saved = repo.rows[response.headers["x-usage-operation-id"]]
    assert saved["state"] == "succeeded"
    replay = route.stream_chat(chat_body(), request)
    assert list(replay.private_content) == [frame] and calls == [True]


def test_actual_tg_route_persists_in_worker_before_delivery(setup, monkeypatch):
    from app.routers import assistant as route
    from app.services.assistant_completion import current_completion_evidence
    request, repo = setup
    request.app.state.settings = NS(tg_assistant_enabled=True, tg_assistant_api_key="fake",
        tg_assistant_base_url="https://unused.invalid", tg_assistant_model="fake", ai_proxy_url="",
        tg_assistant_transport="chat_completions")
    calls = []
    def service(**_):
        calls.append(True)
        terminal_answer(current_completion_evidence())
        yield route.TgAssistantEvent("done", {"message": "thread completed"})
    monkeypatch.setattr(route, "stream_tg_assistant_events", service)
    response = route.stream_tg_chat(chat_body(), request)
    assert "event: meta" in next(response.private_content)
    assert "event: done" in next(response.private_content)
    response.private_content.close()
    saved = repo.rows[response.headers["x-usage-operation-id"]]
    assert saved["state"] == "succeeded"
    request.app.state.settings.tg_assistant_enabled = False
    replay = route.stream_tg_chat(chat_body(), request)
    assert "thread completed" in "".join(replay.private_content)
    assert calls == [True]


def test_summary_sse_saves_before_done_and_cache_retains_original_operation(setup, monkeypatch):
    request, repo = setup
    store, record = summary_store(request)
    monkeypatch.setattr(summary_adapter, "admission", lambda _: __import__('contextlib').nullcontext())
    monkeypatch.setattr(summary_adapter, "authorize_memory_start", lambda _: True)
    calls = []
    async def generate(events, _settings):
        calls.append(True)
        from app.services.assistant_completion import current_completion_evidence
        terminal_answer(current_completion_evidence(), phase="summary")
        yield "summary text"
    monkeypatch.setattr(summary_adapter, "stream_knowledge_summary", generate)
    async def check():
        execution = await summary_adapter.prepare_summary(request, "record-1")
        frames = []
        async for event in execution:
            frames.append(event)
            if event[0] == "done":
                assert execution.operation.operation["state"] == "succeeded"
        original = execution.operation.operation["operation_id"]
        request.headers = {"idempotency-key": "round-00002"}
        cached = await summary_adapter.prepare_summary(request, "record-1")
        assert cached.operation.operation["operation_id"] == original
        assert (await cached.json_result())["summary"] == "summary text"
        assert len(calls) == len(repo.rows) == 1
        assert not record.summary_lock.locked()
    asyncio.run(check())


def test_summary_empty_record_is_free_persisted_terminal(setup, monkeypatch):
    request, _ = setup
    _, record = summary_store(request); record.events = []
    monkeypatch.setattr(summary_adapter, "admission", lambda _: __import__('contextlib').nullcontext())
    monkeypatch.setattr(summary_adapter, "authorize_memory_start", lambda _: True)
    async def check():
        execution = await summary_adapter.prepare_summary(request, "record-1")
        result = await execution.json_result()
        assert result["generated"] is False
        assert execution.operation.operation["outcome"] == "no_events"
        assert execution.operation.operation["state"] == "released"
    asyncio.run(check())


def test_summary_disconnect_retains_unknown_and_releases_only_memory_lock(setup, monkeypatch):
    request, _ = setup
    _, record = summary_store(request)
    monkeypatch.setattr(summary_adapter, "admission", lambda _: __import__('contextlib').nullcontext())
    monkeypatch.setattr(summary_adapter, "authorize_memory_start", lambda _: True)
    async def generate(events, _settings):
        from app.services.assistant_completion import current_completion_evidence
        evidence = current_completion_evidence()
        evidence.begin_provider("summary", {"max_tokens": 20})
        yield "partial"
        yield "never reached"
    monkeypatch.setattr(summary_adapter, "stream_knowledge_summary", generate)
    async def check():
        execution = await summary_adapter.prepare_summary(request, "record-1")
        assert (await execution.__anext__())[0] == "status"
        assert (await execution.__anext__())[0] == "delta"
        await execution.aclose()
        assert execution.operation.operation["state"] == "unknown"
        assert not record.summary_lock.locked() and record.summary is None
    asyncio.run(check())


def test_explicit_skill_error_is_a_durable_zero_result_not_unknown(setup):
    request, _ = setup
    op = operation(request)
    def source():
        terminal_answer(op.evidence, phase="intent")
        yield adapter.sse("skill_error", {"detail": "validated tool failure"})
        yield adapter.sse("done", {"message": "failure"})
    frames = list(adapter.wrap_chat(source(), op))
    assert len(frames) == 2
    assert op.operation["state"] == "released" and op.operation["outcome"] == "failed"
    assert "error" in operation(request).replay


def test_terminal_write_failure_does_not_deliver_done_or_run_again(setup, monkeypatch):
    request, repo = setup
    op = operation(request)
    def fail(**_): raise RuntimeError("test repository unavailable")
    monkeypatch.setattr(repo, "settle", fail)
    def source():
        terminal_answer(op.evidence)
        yield adapter.sse("done", {"message": "not yet durable"})
    with pytest.raises(RuntimeError): next(adapter.wrap_chat(source(), op))
    assert repo.rows[op.operation["operation_id"]]["state"] == "running"
    with pytest.raises(HTTPException): operation(request)


def test_concurrent_same_key_can_reserve_only_one_logical_round(setup):
    from threading import Barrier
    request, repo = setup
    barrier, accepted, rejected = Barrier(2), [], []
    original = repo.lookup
    def lookup(**kw):
        found = original(**kw)
        if found is None:
            barrier.wait(2)
        return found
    # Limit the barrier to the request lookup, not reserve's internal atomic lookup.
    def run():
        with user_context(Identity(OWNER)):
            key = request.headers["idempotency-key"]
            payload = {"text": "hello"}
            original(owner_user_id=OWNER, module="assistant", idempotency_key=key,
                     request_fingerprint=adapter.request_fingerprint(payload))
            barrier.wait(2)
            try: accepted.append(operation(request))
            except HTTPException as exc: rejected.append(exc.status_code)
    threads = [Thread(target=run) for _ in range(2)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(3)
    assert all(not thread.is_alive() for thread in threads)
    assert len(accepted) == 1 and rejected == [409] and len(repo.rows) == 1


def test_legacy_cache_never_reserves_or_calls_provider_even_without_new_policy(setup, monkeypatch):
    request, repo = setup
    store, record = summary_store(request)
    record.summary = {"summary": "old owned cache", "generated": True}
    request.app.state.entitlement_policy = None
    monkeypatch.setattr(repo, "reserve", lambda **_: pytest.fail("legacy cache must not reserve"))
    monkeypatch.setattr(summary_adapter, "generate_knowledge_summary", lambda *_: pytest.fail("no provider"))
    async def check():
        execution = await summary_adapter.prepare_summary(request, "record-1")
        assert execution.headers == {"X-Usage-Legacy-Cache": "true"}
        assert (await execution.json_result())["summary"] == "old owned cache"
        assert not record.summary_lock.locked()
        assert not repo.rows
    asyncio.run(check())


def test_legacy_cache_still_requires_store_owner_authorization(setup):
    request, repo = setup
    store, _ = summary_store(request)
    def denied(*_a, **_kw): raise HTTPException(404)
    store.get_recording = denied
    with pytest.raises(HTTPException) as exc: asyncio.run(summary_adapter.prepare_summary(request, "other-owner-record"))
    assert exc.value.status_code == 404 and not repo.rows


def test_tg_persists_dispatch_before_thread_start_and_start_failure_never_executes(setup, monkeypatch):
    from app.routers import assistant as route
    request, repo = setup
    request.app.state.settings = NS(tg_assistant_enabled=True, tg_assistant_api_key="fake",
        tg_assistant_base_url="https://unused.invalid", tg_assistant_model="fake", ai_proxy_url="")
    monkeypatch.setattr(route, "stream_tg_assistant_events", lambda **_: pytest.fail("worker must not execute"))
    def start(_thread):
        assert next(iter(repo.rows.values()))["state"] == "dispatching"
        raise RuntimeError("thread start rejected")
    monkeypatch.setattr(route.threading.Thread, "start", start)
    response = route.stream_tg_chat(chat_body(), request)
    next(response.private_content)  # metadata only, still reserved
    with pytest.raises(RuntimeError): next(response.private_content)
    row = next(iter(repo.rows.values()))
    assert row["state"] == "released"
    assert not any(name == "claim_running" for name, _ in repo.calls)
