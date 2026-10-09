"""Provider observations only: no network, database, model or paid API access."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar, copy_context
from threading import Thread
from types import SimpleNamespace as NS

import pytest

from app.services import usage_provider as usage


@pytest.fixture
def attempts(monkeypatch):
    rows = []

    class Span:
        def __init__(self, module, kind):
            self.module, self.kind = module, kind
            self.started = False
            self.results = []
            rows.append(self)

        def start(self, *, bind=False):
            assert bind is False
            self.started = True

        def finish(self, *, status, metrics):
            self.results.append({"status": status, "metrics": dict(metrics)})

    monkeypatch.setattr(usage, "current_usage_context", lambda: NS(module="assistant"))
    monkeypatch.setattr(usage, "usage_span", lambda module, *, kind: Span(module, kind))
    return rows


def chat_usage(**overrides):
    value = {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26,
             "prompt_tokens_details": {"cached_tokens": 4},
             "completion_tokens_details": {"reasoning_tokens": 3}}
    value.update(overrides)
    return value


def chat_response(content="{}", **overrides):
    return NS(usage=NS(**chat_usage(**overrides)),
              choices=[NS(message=NS(content=content))])


def assert_tokens(row, inputs=17, outputs=9, total=26):
    metrics = row.results[-1]["metrics"]
    assert [metrics[k] for k in ("provider_input_tokens", "provider_output_tokens", "provider_total_tokens")] == [inputs, outputs, total]
    assert type(metrics["observed_wall_ms"]) is int and metrics["observed_wall_ms"] >= 0
    assert set(metrics) == {*usage._TOKEN_FIELDS, "observed_wall_ms"}


def test_sync_observes_real_usage_once_without_payload_metadata(attempts):
    sent = []
    secret = "secret-key-private-image-private-prompt"
    def create(**kwargs):
        sent.append(kwargs)
        return chat_response(secret)
    response = usage.provider_call(create, model=secret, messages=[secret], api_key=secret)
    assert response.choices[0].message.content == secret
    assert len(sent) == len(attempts) == 1
    assert attempts[0].kind == "provider"
    assert attempts[0].results[0]["status"] == "completed"
    assert_tokens(attempts[0])
    assert secret not in repr(attempts[0].results)
    assert attempts[0].results[0]["metrics"]["cached_input_tokens"] == 4
    assert attempts[0].results[0]["metrics"]["reasoning_tokens"] == 3


@pytest.mark.parametrize("value", [None, -1, True, 1.0, "12", float("nan"), 2**63])
def test_invalid_counts_stay_unknown_without_estimation(attempts, value):
    usage.provider_call(lambda **_: NS(usage={"prompt_tokens": value}, choices=[]), max_tokens=4000)
    assert_tokens(attempts[0], None, None, None)


def test_real_zero_is_preserved_and_missing_total_is_not_summed(attempts):
    usage.provider_call(lambda **_: chat_response(prompt_tokens=0, total_tokens=None))
    assert_tokens(attempts[0], 0, 9, None)


@pytest.mark.parametrize("error,status", [(RuntimeError("private body and key"), "failed"),
    (TimeoutError("private URL"), "timed_out"), (asyncio.CancelledError(), "interrupted")])
def test_failed_create_never_retries_or_turns_unknown_into_zero(attempts, error, status):
    calls = []
    def create(**_):
        calls.append(1)
        raise error
    with pytest.raises(type(error)):
        usage.provider_call(create, stream=True)
    assert len(calls) == len(attempts) == 1
    assert attempts[0].results[0]["status"] == status
    assert_tokens(attempts[0], None, None, None)
    assert "private" not in repr(attempts[0].results)


def test_chat_usage_only_chunk_overwrites_absolute_counts_not_sums(attempts):
    chunks = [NS(choices=[NS(delta=NS(content="x"), finish_reason=None)], usage=None),
              NS(choices=[NS(finish_reason="stop")], usage=chat_usage(completion_tokens=8, total_tokens=25)),
              NS(choices=[], usage=chat_usage()), NS(choices=[], usage=None)]
    stream = usage.provider_call(lambda **_: iter(chunks), stream=True)
    assert list(stream) == chunks
    stream.close()
    assert len(attempts[0].results) == 1
    assert attempts[0].results[0]["status"] == "completed"
    assert_tokens(attempts[0])


def test_early_stream_close_in_other_context_is_interrupted(attempts):
    closed = []
    def chunks():
        try:
            yield NS(choices=[NS(finish_reason=None)], usage=None)
            yield NS(choices=[], usage=chat_usage())
        finally:
            closed.append(True)
    stream = usage.provider_call(lambda **_: chunks(), stream=True)
    next(stream)
    assert attempts[0].results == []
    thread = Thread(target=copy_context().run, args=(stream.close,))
    thread.start()
    thread.join()
    assert closed == [True]
    assert attempts[0].results[0]["status"] == "interrupted"
    assert_tokens(attempts[0], None, None, None)


def test_stream_error_does_not_finalize_until_transport_close(attempts):
    closed = []
    class Stream:
        def __iter__(self): return self
        def __next__(self): raise TimeoutError("secret")
        def close(self): closed.append(True)
    stream = usage.provider_call(lambda **_: Stream(), stream=True)
    with pytest.raises(TimeoutError):
        next(stream)
    assert attempts[0].results == []
    stream.close()
    assert closed == [True]
    assert attempts[0].results[0]["status"] == "timed_out"


@pytest.mark.parametrize("event,status", [("response.completed", "completed"),
    ("response.incomplete", "failed"), ("response.failed", "failed")])
def test_responses_terminal_event_captures_usage_before_business_parsing(attempts, event, status):
    data = {"input_tokens": 7, "output_tokens": 8, "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 2}, "output_tokens_details": {"reasoning_tokens": 5}}
    stream = usage.provider_call(lambda **_: iter([{"type": event, "response": {"usage": data}}]),
                                 usage_api="responses", stream=True)
    next(stream)
    stream.close()
    assert attempts[0].results[0]["status"] == status
    assert_tokens(attempts[0], 7, 8, 15)
    assert attempts[0].results[0]["metrics"]["reasoning_tokens"] == 5


def test_assistant_bad_json_keeps_completed_usage(monkeypatch, attempts):
    from app.services import assistant_chat
    closed = []
    client = NS(chat=NS(completions=NS(create=lambda **_: chat_response("not JSON"))), close=lambda: closed.append(True))
    monkeypatch.setattr(assistant_chat, "create_openai_client", lambda **_: client)
    with pytest.raises(assistant_chat.AssistantChatModelError):
        assistant_chat.complete_assistant_intent(messages=[], modules=[], active_module=None, skill_catalog="",
            api_key="fake", base_url="https://unused.invalid", model="fake")
    assert closed == [True]
    assert attempts[0].results[0]["status"] == "completed"
    assert_tokens(attempts[0])


def test_assistant_generator_close_finalizes_child_and_client(monkeypatch, attempts):
    from app.services import assistant_chat
    closed = []
    client = NS(chat=NS(completions=NS(create=lambda **_: iter([
        NS(choices=[NS(delta=NS(content="x"), finish_reason=None)], usage=None),
        NS(choices=[], usage=chat_usage())]))), close=lambda: closed.append(True))
    monkeypatch.setattr(assistant_chat, "create_openai_client", lambda **_: client)
    stream = assistant_chat.stream_assistant_chat(messages=[], modules=[], active_module=None,
            api_key="fake", base_url="https://unused.invalid", model="fake")
    assert next(stream) == "x"
    stream.close()
    assert closed == [True]
    assert attempts[0].results[0]["status"] == "interrupted"


def test_extractor_parse_failure_keeps_usage(monkeypatch, attempts):
    from app.services.online_knowledge import extractor
    client = NS(chat=NS(completions=NS(create=lambda **_: chat_response("bad JSON"))), close=lambda: None)
    monkeypatch.setattr(extractor, "create_openai_client", lambda **_: client)
    engine = extractor.PolymerExtractor("fake", "https://unused.invalid", "fake")
    result = engine.extract_from_abstract("private title", "private abstract", "private paper", "synthesis")
    assert "error" in result
    assert attempts[0].results[0]["status"] == "completed"
    assert_tokens(attempts[0])
    assert "private" not in repr(attempts[0].results)


def test_responses_fallback_creates_two_independent_attempts(monkeypatch, attempts):
    from app.services import tg_assistant
    from app.models import AssistantChatMessage
    calls = []
    class Unsupported(Exception): status_code = 404
    def responses(**_):
        calls.append("responses")
        raise Unsupported("private body")
    def chat(**_):
        calls.append("chat")
        return chat_response('{"type":"chat"}')
    client = NS(responses=NS(create=responses), chat=NS(completions=NS(create=chat)), close=lambda: None)
    monkeypatch.setattr(tg_assistant, "create_openai_client", lambda **_: client)
    monkeypatch.setattr(tg_assistant, "_RESPONSES_UNSUPPORTED", set())
    list(tg_assistant._stream_intent(messages=[AssistantChatMessage(role="user", content="private")],
        context_payload=None, api_key="fake", base_url="https://unused.invalid", model="fake", proxy_url="",
        image_inputs=(), reasoning_effort="medium", transport="auto", cancelled=None))
    assert calls == ["responses", "chat"]
    assert len(attempts) == 2
    assert [a.results[0]["status"] for a in attempts] == ["failed", "completed"]
    assert_tokens(attempts[0], None, None, None)
    assert_tokens(attempts[1])


def summary_mock(monkeypatch, *, body=None, frames=None):
    from app.services import knowledge_poc_summary as summary
    calls, closed = [], []
    class Response:
        is_success = True
        async def aiter_bytes(self, **_):
            yield json.dumps(body).encode()
        async def aiter_lines(self):
            for frame in frames:
                yield "data: " + (frame if isinstance(frame, str) else json.dumps(frame))
                yield ""
    class Client:
        def __init__(self, **_): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): closed.append(True)
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            calls.append((args, kwargs))
            yield Response()
    monkeypatch.setattr(summary.httpx, "AsyncClient", Client)
    settings = NS(assistant_api_key="secret-key", assistant_base_url="https://unused.invalid", assistant_model="fake", ai_proxy_url="")
    return summary, settings, [{"event": "search.completed", "query": "private query"}], calls, closed


def test_raw_summary_bad_content_still_keeps_returned_usage(monkeypatch, attempts):
    from fastapi import HTTPException
    summary, settings, events, calls, closed = summary_mock(monkeypatch, body={"usage": chat_usage(), "choices": []})
    with pytest.raises(HTTPException):
        asyncio.run(summary.generate_knowledge_summary(events, settings))
    assert len(calls) == len(closed) == 1
    assert attempts[0].results[0]["status"] == "completed"
    assert_tokens(attempts[0])


def test_raw_summary_usage_only_frame_and_done(monkeypatch, attempts):
    summary, settings, events, calls, closed = summary_mock(monkeypatch, frames=[
        {"choices": [{"delta": {"content": "answer"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": chat_usage()}, "[DONE]"])
    async def run(): return [text async for text in summary.stream_knowledge_summary(events, settings)]
    assert asyncio.run(run()) == ["answer"]
    assert calls[0][1]["json"]["stream_options"] == {"include_usage": True}
    assert closed == [True]
    assert_tokens(attempts[0])
    assert attempts[0].results[0]["status"] == "completed"


def test_raw_summary_cancel_keeps_unknown_usage(monkeypatch, attempts):
    summary, settings, events, calls, closed = summary_mock(monkeypatch, frames=[
        {"choices": [{"delta": {"content": "answer"}, "finish_reason": None}]},
        {"choices": [], "usage": chat_usage()}])
    async def run():
        stream = summary.stream_knowledge_summary(events, settings)
        assert await anext(stream) == "answer"
        assert attempts[0].results == []
        await stream.aclose()
    asyncio.run(run())
    assert closed == [True]
    assert attempts[0].results[0]["status"] == "interrupted"
    assert_tokens(attempts[0], None, None, None)


def test_literature_threads_propagate_context_and_http_has_no_content(monkeypatch, attempts):
    from app.services.online_knowledge import literature_searcher as literature
    owner = ContextVar("test_owner", default=None)
    observed = []
    def get(*args, **kwargs):
        observed.append(owner.get())
        return NS(status_code=200, json=lambda: {"results": []})
    monkeypatch.setattr(literature.requests, "get", get)
    token = owner.set("owner-A")
    try:
        assert literature.SimpleLiteratureSearcher().search_all("private query", ["openalex"], 1) == []
    finally:
        owner.reset(token)
    assert observed == ["owner-A"]
    assert attempts[0].results[0]["status"] == "completed"
    assert_tokens(attempts[0], None, None, None)


def test_client_retry_policy_stays_zero(monkeypatch):
    from app.services import ai_client
    seen = []
    monkeypatch.setattr(ai_client, "DefaultHttpxClient", lambda **_: object())
    monkeypatch.setattr(ai_client, "OpenAI", lambda **kwargs: seen.append(kwargs))
    ai_client.create_openai_client(api_key="fake", base_url="https://unused.invalid")
    assert seen[0]["max_retries"] == 0


class RecordingRepository:
    def __init__(self):
        self.events = []
    def record_event(self, **event):
        self.events.append(event)


def test_real_context_preserves_owner_parent_and_distinct_attempts():
    from app.usage_context import UsageCollector, UsageContext, usage_scope
    repository = RecordingRepository()
    collector = UsageCollector(enabled=True, repository=repository)
    parent = UsageContext("11111111-1111-1111-1111-111111111111", "assistant", "business-task")
    with usage_scope(parent, collector=collector):
        with pytest.raises(ValueError):
            usage.provider_call(lambda **_: (_ for _ in ()).throw(ValueError("secret")))
        usage.provider_call(lambda **_: chat_response())
    assert len(repository.events) == 4
    first, second = repository.events[0], repository.events[2]
    assert first["task_id"] != second["task_id"]
    for event in repository.events:
        assert event["owner_user_id"] == parent.owner_user_id
        assert event["parent_task_id"] == event["root_task_id"] == parent.task_id
        assert event["kind"] == "provider"
        assert event["attempt_id"] == "1"
        assert event["module"] == "assistant"
        assert "secret" not in repr(event)
    assert [row["status"] for row in repository.events] == ["running", "failed", "running", "completed"]
    assert repository.events[1]["metrics"]["provider_total_tokens"] is None
    assert repository.events[3]["metrics"]["provider_total_tokens"] == 26


def test_real_context_root_waits_for_provider_close_outside_request_scope():
    from app.usage_context import UsageCollector, UsageContext, UsageSpan, usage_scope
    repository = RecordingRepository()
    collector = UsageCollector(enabled=True, repository=repository)
    context = UsageContext("11111111-1111-1111-1111-111111111111", "assistant", "root")
    root_span = UsageSpan(context, collector).start()
    with usage_scope(context, collector=collector):
        stream = usage.provider_call(lambda **_: iter([NS(choices=[], usage=None)]), stream=True)
        next(stream)
        root_span.finish("interrupted")
    assert [row["status"] for row in repository.events] == ["running", "running"]
    thread = Thread(target=stream.close)
    thread.start()
    thread.join()
    assert [row["status"] for row in repository.events] == ["running", "running", "interrupted", "interrupted"]
    assert repository.events[-1]["task_id"] == "root"
    assert repository.events[-2]["metrics"]["provider_total_tokens"] is None


def test_no_owner_context_does_not_invent_owner_or_write():
    from app.usage_context import UsageCollector, usage_scope
    repository = RecordingRepository()
    calls = []
    with usage_scope(None, collector=UsageCollector(enabled=True, repository=repository)):
        usage.provider_call(lambda **_: calls.append(1) or chat_response())
    assert calls == [1]
    assert repository.events == []


def test_start_write_failure_prevents_provider_invocation():
    from app.usage_context import UsageCollector, UsageContext, UsageMeteringUnavailable, usage_scope
    calls = []
    class BrokenRepository:
        def record_event(self, **_): raise RuntimeError("private DSN")
    context = UsageContext("11111111-1111-1111-1111-111111111111", "assistant", "root")
    with usage_scope(context, collector=UsageCollector(enabled=True, repository=BrokenRepository())):
        with pytest.raises(UsageMeteringUnavailable, match="temporarily unavailable"):
            usage.provider_call(lambda **_: calls.append(1))
    assert calls == []


def test_terminal_retry_replays_only_metadata_not_provider_call():
    from app.usage_context import UsageCollector, UsageContext, usage_scope
    class RetryRepository(RecordingRepository):
        def __init__(self):
            super().__init__()
            self.failed = None
        def record_event(self, **event):
            if event["status"] == "completed" and self.failed is None:
                self.failed = event
                raise RuntimeError("private DSN")
            super().record_event(**event)
    repository = RetryRepository()
    collector = UsageCollector(enabled=True, repository=repository)
    context = UsageContext("11111111-1111-1111-1111-111111111111", "assistant", "root")
    calls = []
    with usage_scope(context, collector=collector):
        usage.provider_call(lambda **_: calls.append(1) or chat_response())
        assert collector.snapshot()["pending_terminal_events"] == 1
        usage.provider_call(lambda **_: calls.append(1) or chat_response())
    assert calls == [1, 1]
    assert collector.snapshot()["pending_terminal_events"] == 0
    replayed = [row for row in repository.events if row["task_id"] == repository.failed["task_id"] and row["status"] == "completed"]
    assert replayed == [repository.failed]
    assert replayed[0]["metrics"]["provider_total_tokens"] == 26



def test_raw_summary_missing_done_preserves_observed_usage_without_claiming_completion(monkeypatch, attempts):
    from fastapi import HTTPException
    summary, settings, events, _, closed = summary_mock(monkeypatch, frames=[
        {"choices": [{"delta": {"content": "answer"}, "finish_reason": None}]},
        {"choices": [], "usage": chat_usage()}])
    async def run(): return [part async for part in summary.stream_knowledge_summary(events, settings)]
    with pytest.raises(HTTPException):
        asyncio.run(run())
    assert closed == [True]
    assert attempts[0].results[0]["status"] == "interrupted"
    assert_tokens(attempts[0])


def test_raw_summary_timeout_uses_fixed_status_and_never_retries(monkeypatch, attempts):
    from fastapi import HTTPException
    summary, settings, events, _, _ = summary_mock(monkeypatch)
    calls = []
    class TimeoutClient:
        def __init__(self, **_): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            calls.append(1)
            raise summary.httpx.ReadTimeout("private key and provider body")
            yield
    monkeypatch.setattr(summary.httpx, "AsyncClient", TimeoutClient)
    with pytest.raises(HTTPException) as error:
        asyncio.run(summary.generate_knowledge_summary(events, settings))
    assert error.value.status_code == 504
    assert calls == [1]
    assert attempts[0].results[0]["status"] == "timed_out"
    assert_tokens(attempts[0], None, None, None)
    assert "private" not in repr(attempts[0].results)
