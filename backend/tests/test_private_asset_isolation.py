from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.auth.context import Identity, current_owner_id, user_context
from app.models import KnowledgeDocumentResult, KnowledgeSearchResponse, OnlineKnowledgeSearchRequest
from app.recording_models import ArticleObservation, RecordedKnowledgeSearchRequest, RecordingStart
from app.routers.online_knowledge import resolve_online_model_access
from app.services.browsing_recording import BrowsingRecordingStore
from app.services.in_memory_jobs import BoundedInMemoryJobStore, JobNotFoundError, JobStoreCapacityError
from app.services.online_knowledge.search_service import OnlineKnowledgeConfigError
from app.services.private_execution import bounded_stream, submit_private_job
from app.task_control import acquire_admission, configure_task_control

A = Identity("11111111-1111-1111-1111-111111111111")
B = Identity("22222222-2222-2222-2222-222222222222")


def test_release_logs_keep_creating_identity_and_exclude_session_secrets(caplog):
    import json
    import logging
    original = Identity(A.user_id, session_id="private-session-secret", request_id="request-a")
    with caplog.at_level(logging.INFO, logger="nexpoly.task_control"):
        with user_context(original):
            lease = acquire_admission("reverse", task_type="tg_reverse", task_id="task-a")
        with user_context(B):
            lease.release()
            lease.release()
    events = [json.loads(record.message) for record in caplog.records if record.name == "nexpoly.task_control"]
    assert [event['event'] for event in events] == ['admission_acquired', 'admission_released']
    assert all(event['owner_user_id'] == A.user_id and event['request_id'] == 'request-a'
               and event['task_id'] == 'task-a' for event in events)
    assert 'private-session-secret' not in caplog.text


def test_memory_lookup_and_mutation_hide_other_owner_and_apply_independent_cap():
    store = BoundedInMemoryJobStore(max_user_jobs=1)
    with user_context(A):
        job = store.create("polytao", lambda job_id: {"job_id": job_id, "input": "private"})
        with pytest.raises(JobStoreCapacityError):
            store.create("polytao", lambda job_id: {"job_id": job_id})
    with user_context(B):
        with pytest.raises(JobNotFoundError):
            store.read("polytao", job["job_id"], lambda value: value)
        with pytest.raises(JobNotFoundError):
            store.mutate("polytao", job["job_id"], lambda value: value.clear())
        store.create("polytao", lambda job_id: {"job_id": job_id})
    with user_context(A):
        assert store.read("polytao", job["job_id"], lambda value: value["input"]) == "private"


def test_future_keeps_creator_and_permit_until_real_execution_ends(monkeypatch):
    monkeypatch.setattr("app.task_control._start_checker", lambda _: True)
    running, finish = Event(), Event()
    seen = []

    def work():
        seen.append(current_owner_id())
        running.set()
        finish.wait(3)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with user_context(A):
            future = submit_private_job(executor, work, channel="reverse", on_disabled=lambda: None)
        assert running.wait(2)
        with user_context(A), pytest.raises(HTTPException) as denied:
            acquire_admission("reverse")
        assert denied.value.status_code == 429
        with user_context(B), acquire_admission("reverse"):
            pass
        finish.set()
        future.result(2)
    assert seen == [A.user_id]
    with user_context(A), acquire_admission("reverse"):
        pass


def test_disabled_queued_future_never_invokes_runner(monkeypatch):
    checks = iter((True, False))
    monkeypatch.setattr("app.task_control._start_checker", lambda _: next(checks))
    seen = []
    with ThreadPoolExecutor(max_workers=1) as executor, user_context(A):
        future = submit_private_job(executor, lambda: seen.append("executed"), channel="reverse",
                                    on_disabled=lambda: seen.append("disabled"))
        future.result(2)
    assert seen == ["disabled"]


def test_stream_can_resume_on_distinct_thread_contexts_and_preserve_identity(monkeypatch):
    monkeypatch.setattr("app.task_control._start_checker", lambda _: True)
    @bounded_stream("ai")
    def events():
        yield current_owner_id()
        yield current_owner_id()

    with user_context(A):
        stream = events()
    with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
        assert first.submit(next, stream).result() == A.user_id
        assert second.submit(next, stream).result() == A.user_id
        second.submit(stream.close).result()
    with user_context(A), acquire_admission("ai"):
        pass


@pytest.mark.asyncio
async def test_browsing_snapshot_without_recording_id_is_private_and_same_ids_do_not_alias():
    store = BrowsingRecordingStore(SimpleNamespace())
    async def search():
        return KnowledgeSearchResponse(query="private query", query_time_ms=1, total=1, results=[
            KnowledgeDocumentResult(knowledge_id=1, source_file="public.pdf", source_row_number=1,
                                    title_en="Paper", abstract="Abstract", abstract_snippet="Abstract")])
    with user_context(A):
        await store.start_recording(RecordingStart(recording_id="same"))
        found = await store.search(RecordedKnowledgeSearchRequest(query="private query"), search)
    with user_context(B):
        await store.start_recording(RecordingStart(recording_id="same"))
        with pytest.raises(HTTPException) as denied:
            await store.observe_article(ArticleObservation(search_id=found.search_id, knowledge_id=1,
                                                           source="reaction_tab"))
        assert denied.value.status_code == 404
        await store.stop_recording("same")
    with user_context(A):
        assert store.get_recording("same").ended_at is None
        observed = await store.observe_article(ArticleObservation(search_id=found.search_id, knowledge_id=1,
                                                                  source="reaction_tab"))
        assert observed["query"] == "private query"


def test_platform_credentials_cannot_follow_client_destination_or_model():
    settings = SimpleNamespace(online_knowledge_api_key="server-secret", online_knowledge_base_url="https://api.example.test/v1",
                               online_knowledge_model="approved", online_knowledge_proxy_url="")
    access = resolve_online_model_access(OnlineKnowledgeSearchRequest(material="PI"), settings)
    assert access.base_url == settings.online_knowledge_base_url
    for overrides in ({"base_url": "https://attacker.test/v1"}, {"model": "unapproved"},
                      {"base_url": settings.online_knowledge_base_url}, {"model": settings.online_knowledge_model},
                      {"api_key": "personal"}, {"api_key": None}, {"use_server_default": True}):
        with pytest.raises(ValidationError):
            OnlineKnowledgeSearchRequest(material="PI", **overrides)


def test_stream_grants_once_and_releases_unstarted_or_rejected_stream(monkeypatch):
    active, checks, supplied = [True], [], []
    monkeypatch.setattr('app.task_control._start_checker', lambda owner: checks.append(owner) or active[0])
    @bounded_stream('ai')
    def events():
        supplied.append(True)
        yield 1
        yield 2
    with user_context(A):
        unused = events()
        unused.close()
        with acquire_admission('ai'):
            pass
        stream = events()
        assert next(stream) == 1
        active[0] = False
        assert list(stream) == [2]
        assert checks == [A.user_id]
        denied = events()
        with pytest.raises(HTTPException) as error:
            next(denied)
        assert error.value.status_code == 403
        assert supplied == [True]
        with acquire_admission('ai'):
            pass


def test_gpu_waiter_is_only_authorized_after_actual_slot(monkeypatch):
    from app.auth.context import service_context
    from app.services.gpu_runtime_registry import GpuRuntimeRegistry, GpuSchedulerClosedError
    active, checks, executed = [True], [], []
    waiting = Event()
    monkeypatch.setattr('app.task_control._start_checker', lambda owner: checks.append(owner) or active[0])
    registry = GpuRuntimeRegistry()
    registry.register('test', enabled=True, loader=lambda: object())
    original = registry._acquire_inference
    def acquire(*args, **kwargs):
        if current_owner_id(required=False) is not None:
            waiting.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(registry, '_acquire_inference', acquire)
    def work():
        with registry.inference_session('test', timeout_seconds=3):
            executed.append(True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with service_context(), registry.inference_session('test', timeout_seconds=0):
            with user_context(A):
                future = submit_private_job(executor, work, channel='backend_gpu', on_disabled=lambda: None)
            assert waiting.wait(2)
            assert checks == [A.user_id]  # Submission only; no execution grant yet.
            active[0] = False
        with pytest.raises(GpuSchedulerClosedError):
            future.result(2)
    assert checks == [A.user_id, A.user_id] and executed == []
    with user_context(A), acquire_admission('backend_gpu'):
        pass


def test_direct_skill_prediction_shares_cpu_budget_and_final_start_check(monkeypatch, tmp_path):
    from app.services import predictor
    from app.services.assistant_skills.predict_properties import build_predict_properties_skill
    from app.services.assistant_skills.registry import AssistantSkillContext
    executed = []
    monkeypatch.setattr(predictor, '_predict_unbounded', lambda *_: executed.append(True) or {'Glass transition temperature': 50.0})
    monkeypatch.setattr('app.task_control._start_checker', lambda _: True)
    skill = build_predict_properties_skill()
    arguments = skill.validate_arguments({'smiles':'CC', 'properties':['Tg']})
    context = AssistantSkillContext(model_enabled=True,model_dir=tmp_path)
    with user_context(A):
        with acquire_admission('cpu'), pytest.raises(HTTPException) as error:
            skill.execute(arguments, context)
        assert error.value.status_code == 429 and executed == []
        assert skill.execute(arguments, context)['predictions']['Glass transition temperature'] == 50
        monkeypatch.setattr('app.task_control._start_checker', lambda _: False)
        with pytest.raises(HTTPException) as disabled:
            predictor.predict('CC', ['Glass transition temperature'])
        assert disabled.value.status_code == 403 and executed == [True]
        with acquire_admission('cpu'):
            pass


@pytest.mark.asyncio
async def test_http_disconnect_keeps_lease_until_inflight_pull_stops_then_closes(monkeypatch):
    import asyncio
    import anyio
    from app.services.private_execution import PrivateStreamingResponse
    monkeypatch.setattr('app.task_control._start_checker', lambda _: True)
    started, finish = Event(), Event()
    closed = []
    disconnect = asyncio.Event()
    @bounded_stream('ai')
    def events():
        try:
            started.set()
            finish.wait(3)
            yield 'data: first\n\n'
            yield 'data: second\n\n'
        finally:
            closed.append(True)
    async def receive():
        await disconnect.wait()
        return {'type':'http.disconnect'}
    async def send(_message):
        pass
    with user_context(A):
        response = PrivateStreamingResponse(events(), media_type='text/event-stream')
    response_task = asyncio.create_task(response({'type':'http'}, receive, send))
    assert await anyio.to_thread.run_sync(started.wait, 2)
    disconnect.set()
    await asyncio.sleep(0)
    with user_context(A), pytest.raises(HTTPException) as occupied:
        acquire_admission('ai')
    assert occupied.value.status_code == 429
    finish.set()
    await asyncio.wait_for(response_task, 2)
    assert closed == [True]
    with user_context(A), acquire_admission('ai'):
        pass


def test_disabled_submission_and_oversized_model_output_return_all_permits(monkeypatch):
    monkeypatch.setattr('app.task_control._start_checker', lambda _: False)
    with ThreadPoolExecutor(max_workers=1) as executor, user_context(A):
        with pytest.raises(HTTPException) as denied:
            submit_private_job(executor, lambda: pytest.fail('disabled task ran'), channel='backend_gpu', on_disabled=lambda: None)
        assert denied.value.status_code == 403
        with acquire_admission('backend_gpu'):
            pass
    monkeypatch.setattr('app.task_control._start_checker', lambda _: True)
    monkeypatch.setenv('PRIVATE_MODEL_OUTPUT_BYTES', '100')
    @bounded_stream('ai')
    def events():
        yield 'x' * 200
    with user_context(A):
        with pytest.raises(HTTPException) as oversized:
            next(events())
        assert oversized.value.status_code == 413
        with acquire_admission('ai'):
            pass


@pytest.mark.asyncio
async def test_summary_rejects_provider_body_before_retaining_unbounded_json(monkeypatch):
    import httpx
    from app.services import knowledge_poc_summary as summaries
    original = httpx.AsyncClient
    def provider(request):
        return httpx.Response(200, json={'choices':[{'message':{'content':'x'*1000},'finish_reason':'stop'}]})
    monkeypatch.setattr(summaries.httpx, 'AsyncClient', lambda **options: original(transport=httpx.MockTransport(provider), **options))
    monkeypatch.setenv('PRIVATE_SUMMARY_OUTPUT_BYTES','100')
    settings = SimpleNamespace(assistant_base_url='https://provider.example/v1',assistant_api_key='test-key',
                               assistant_model='approved',ai_proxy_url='')
    with pytest.raises(HTTPException) as oversized:
        await summaries.generate_knowledge_summary([{'event':'search.failed','query':'x'}],settings)
    assert oversized.value.status_code == 413


@pytest.mark.asyncio
async def test_browsing_event_append_obeys_total_user_byte_quota(monkeypatch):
    monkeypatch.setenv('PRIVATE_BROWSING_BYTES_PER_USER', '4000')
    store = BrowsingRecordingStore(SimpleNamespace())
    with user_context(A):
        await store.start_recording(RecordingStart(recording_id='a'))
        record = store.get_recording('a')
        with pytest.raises(HTTPException) as oversized:
            record.append({'event':'observation','content':'x'*4000})
        assert oversized.value.status_code == 413
        assert record.events == []
    with user_context(B):
        await store.start_recording(RecordingStart(recording_id='b'))
        store.get_recording('b').append({'event':'observation','content':'small'})
