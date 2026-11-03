"""Browsing-summary HTTP contracts; all provider I/O is an in-process mock."""
import asyncio
import json
import socket
from contextlib import nullcontext

import httpx
import pytest
from fastapi import HTTPException

from app import config
from app.auth.context import Identity, user_context
from app.config import Settings
from app.services import knowledge_poc_summary as summary, usage_provider
from app.services.assistant_completion import CompletionEvidence, completion_scope


EVENTS = [{"event": "search.completed", "query": "polyimide", "sequence": 1}]
SECRET = "test-secret-key"
USAGE = {"input_tokens": 17, "output_tokens": 9, "total_tokens": 26,
         "input_tokens_details": {"cached_tokens": 4}, "output_tokens_details": {"reasoning_tokens": 3}}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEFAULT_ENV_FILE", tmp_path / "absent.env")
    for key in ("AI_PROXY_URL", "AI_MODEL_PROXY_URL", "ONLINE_KNOWLEDGE_PROXY_URL", "ASSISTANT_TRANSPORT",
                "PRIVATE_SUMMARY_OUTPUT_BYTES", "PRIVATE_SUMMARY_EVIDENCE_CHARACTERS", "PRIVATE_SUMMARY_MAX_TOKENS"):
        monkeypatch.delenv(key, raising=False)
    def denied(*_, **__):
        raise AssertionError("Summary tests must not access real network/database sockets")
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def spans(monkeypatch):
    rows = []
    class Span:
        def start(self, **_): pass
        def finish(self, *, status, metrics): rows.append({"status": status, "metrics": dict(metrics)})
    monkeypatch.setattr(usage_provider, "usage_span", lambda *_, **__: Span())
    return rows


def settings(transport="responses", **kwargs):
    return Settings(assistant_api_key=SECRET, assistant_base_url="https://model.invalid/v1",
                    assistant_model="configured-model", assistant_transport=transport,
                    ai_proxy_url="http://literature-only.invalid:17893", **kwargs)


def completed(text="回顾正文", status="completed"):
    return {"status": status, "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": USAGE}


def terminal(text="回顾正文", status="completed"):
    return {"type": "response." + status, "response": completed(text, status)}


def delta(text):
    return {"type": "response.output_text.delta", "delta": text}


def sse(events):
    return "".join("data: " + (event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)) + "\n\n"
                   for event in events).encode()


def install_provider(monkeypatch, responses):
    calls, options = [], []
    original = httpx.AsyncClient
    def handler(request):
        calls.append({"url": str(request.url), "payload": json.loads(request.content)})
        value = responses.pop(0)
        if isinstance(value, Exception): raise value
        return value
    def client(**kwargs):
        options.append(dict(kwargs))
        # Assert the chosen route separately; prevent a real proxy transport.
        kwargs.pop("proxy", None)
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(summary.httpx, "AsyncClient", client)
    return calls, options


async def invoke(mode, value, **kwargs):
    if mode == "json":
        return (await summary.generate_knowledge_summary(EVENTS, value, **kwargs))["summary"]
    return "".join([part async for part in summary.stream_knowledge_summary(EVENTS, value, **kwargs)])


@pytest.mark.parametrize("mode", ["json", "stream"])
@pytest.mark.parametrize("transport", ["chat_completions", "responses"])
@pytest.mark.parametrize("proxy", [None, "", "http://model-only.invalid:17892"])
def test_summary_uses_selected_model_route_and_protocol(monkeypatch, spans, mode, transport, proxy):
    if transport == "responses":
        response = httpx.Response(200, json=completed()) if mode == "json" else httpx.Response(200, content=sse([delta("回顾正文"), terminal()]))
    else:
        usage = {"prompt_tokens": 17, "completion_tokens": 9, "total_tokens": 26}
        body = {"choices": [{"message": {"content": "回顾正文"}, "finish_reason": "stop"}], "usage": usage}
        response = httpx.Response(200, json=body) if mode == "json" else httpx.Response(200, content=sse([
            {"choices": [{"delta": {"content": "回顾正文"}, "finish_reason": "stop"}]}, {"choices": [], "usage": usage}, "[DONE]"]))
    calls, options = install_provider(monkeypatch, [response])
    value = settings(transport, ai_model_proxy_url=proxy)
    evidence = CompletionEvidence(1, 4096)
    with completion_scope(evidence):
        assert asyncio.run(invoke(mode, value)) == "回顾正文"
    assert len(calls) == len(options) == len(spans) == 1
    assert options[0]["proxy"] == (proxy or None if proxy is not None else value.ai_proxy_url)
    assert options[0]["trust_env"] is False and options[0]["follow_redirects"] is False
    assert calls[0]["url"] == "https://model.invalid/v1/" + ("responses" if transport == "responses" else "chat/completions")
    payload = calls[0]["payload"]
    assert payload["model"] == "configured-model" and payload["stream"] is (mode == "stream")
    if transport == "responses":
        assert payload["max_output_tokens"] == 4096 and payload["store"] is False
        assert payload["reasoning"] == {"effort": "medium"}
        assert payload["input"][0]["role"] == "user" and "polyimide" in payload["input"][0]["content"]
        assert not set(payload) & {"messages", "max_tokens", "stream_options"}
    else:
        assert payload["max_tokens"] == 4096 and "messages" in payload
    assert spans[0]["status"] == "completed"
    assert spans[0]["metrics"]["provider_total_tokens"] == 26
    assert evidence.facts(True)["completion_proven"] and evidence.validated
    assert SECRET not in repr(spans) and "polyimide" not in repr(spans)


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress", None])
def test_json_noncompleted_responses_keeps_usage_and_fails(monkeypatch, spans, status):
    calls, _ = install_provider(monkeypatch, [httpx.Response(200, json=completed(status=status))])
    with pytest.raises(HTTPException) as error:
        asyncio.run(invoke("json", settings()))
    assert error.value.status_code == 502 and "记录已保留" in error.value.detail
    assert len(calls) == len(spans) == 1
    assert spans[0]["status"] == "failed" and spans[0]["metrics"]["provider_total_tokens"] == 26


@pytest.mark.parametrize("events", [
    [delta("partial")], [delta("partial"), "[DONE]"], [delta("partial"), terminal("partial", "incomplete")],
    [delta("partial"), terminal("partial", "failed")], [delta("partial"), {"type": "error", "message": "private provider body"}],
    [delta("partial"), terminal("different")], [terminal("")], [{"type": "response.completed"}],
    [{"type": "response.refusal.delta", "delta": "refused"}], [terminal(), delta("late")],
    [terminal(), terminal()], ["not JSON"], ["[]"],
])
def test_stream_rejects_incomplete_malformed_and_late_output(monkeypatch, spans, events):
    calls, _ = install_provider(monkeypatch, [httpx.Response(200, content=sse(events))])
    with pytest.raises(HTTPException) as error:
        asyncio.run(invoke("stream", settings()))
    assert error.value.status_code == 502 and "记录已保留" in error.value.detail
    assert "private provider body" not in str(error.value.detail)
    assert len(calls) == len(spans) == 1 and spans[0]["status"] != "completed"


@pytest.mark.parametrize("mode", ["json", "stream"])
@pytest.mark.parametrize("content", [[], [{"type": "refusal", "refusal": "拒绝"}]])
def test_empty_or_refused_response_never_becomes_business_completion(monkeypatch, spans, mode, content):
    body = {**completed(), "output": [{"content": content}]}
    response = httpx.Response(200, json=body) if mode == "json" else httpx.Response(200, content=sse([
        {"type": "response.completed", "response": body}]))
    install_provider(monkeypatch, [response])
    evidence = CompletionEvidence(1, 4096)
    with completion_scope(evidence), pytest.raises(HTTPException):
        asyncio.run(invoke(mode, settings()))
    assert not evidence.validated


@pytest.mark.parametrize("trailer", [[], ["[DONE]"]])
def test_completed_output_fallback_and_optional_done(monkeypatch, spans, trailer):
    install_provider(monkeypatch, [httpx.Response(200, content=sse([terminal(), *trailer]))])
    assert asyncio.run(invoke("stream", settings())) == "回顾正文"
    assert spans[0]["status"] == "completed"
    assert spans[0]["metrics"]["cached_input_tokens"] == 4 and spans[0]["metrics"]["reasoning_tokens"] == 3


def test_stream_redacts_secret_split_across_deltas(monkeypatch, spans):
    text = "正文 " + SECRET + "。"
    install_provider(monkeypatch, [httpx.Response(200, content=sse([delta("正文 test-"), delta("secret-"), delta("key。"), terminal(text)]))])
    assert asyncio.run(invoke("stream", settings())) == "正文 [已隐藏]。"
    assert SECRET not in repr(spans)


def test_json_redacts_secret_and_absent_provider_usage_stays_unknown(monkeypatch, spans):
    body = completed("正文 " + SECRET)
    body.pop("usage")
    install_provider(monkeypatch, [httpx.Response(200, json=body)])
    assert asyncio.run(invoke("json", settings())) == "正文 [已隐藏]"
    assert spans[0]["metrics"]["provider_total_tokens"] is None


def test_unterminated_terminal_frame_cannot_complete(monkeypatch, spans):
    install_provider(monkeypatch, [httpx.Response(200, content=sse([delta("partial")]) + b"data: " + json.dumps(terminal("partial")).encode())])
    with pytest.raises(HTTPException) as error:
        asyncio.run(invoke("stream", settings()))
    assert error.value.status_code == 502 and spans[0]["status"] == "failed"


@pytest.mark.parametrize("mode", ["json", "stream"])
def test_evidence_limit_rejects_before_provider(monkeypatch, spans, mode):
    monkeypatch.setenv("PRIVATE_SUMMARY_EVIDENCE_CHARACTERS", "1")
    calls, _ = install_provider(monkeypatch, [])
    with pytest.raises(HTTPException) as error:
        asyncio.run(invoke(mode, settings()))
    assert error.value.status_code == 413 and calls == spans == []


@pytest.mark.parametrize("mode", ["json", "stream"])
def test_provider_failure_is_redacted_without_retry(monkeypatch, spans, mode):
    calls, _ = install_provider(monkeypatch, [httpx.Response(403, json={"error": SECRET})])
    with pytest.raises(HTTPException) as error:
        asyncio.run(invoke(mode, settings()))
    assert error.value.status_code == 502 and SECRET not in error.value.detail
    assert len(calls) == len(spans) == 1 and spans[0]["status"] == "failed"


@pytest.mark.parametrize("mode", ["json", "stream"])
def test_response_bytes_are_bounded(monkeypatch, spans, mode):
    monkeypatch.setenv("PRIVATE_SUMMARY_OUTPUT_BYTES", "100")
    response = httpx.Response(200, json=completed("x" * 1000)) if mode == "json" else httpx.Response(200, content=sse([delta("x" * 1000)]))
    install_provider(monkeypatch, [response])
    with pytest.raises(HTTPException) as error:
        asyncio.run(invoke(mode, settings()))
    assert error.value.status_code == 413 and spans[0]["status"] != "completed"


def test_stream_text_limit_applies_before_terminal(monkeypatch, spans):
    monkeypatch.setenv("PRIVATE_SUMMARY_OUTPUT_BYTES", "1000")
    install_provider(monkeypatch, [httpx.Response(200, content=sse([delta("x" * 1001)]))])
    with pytest.raises(HTTPException) as error:
        asyncio.run(invoke("stream", settings()))
    assert error.value.status_code == 413 and spans[0]["status"] != "completed"


class DelayedBody(httpx.AsyncByteStream):
    def __init__(self, first=b""):
        self.first, self.closed = first, False
    async def __aiter__(self):
        if self.first: yield self.first
        await asyncio.sleep(1)
        yield b"never delivered"
    async def aclose(self): self.closed = True


@pytest.mark.parametrize("mode", ["json", "stream"])
def test_total_deadline_closes_provider_and_records_timeout(monkeypatch, spans, mode):
    body = DelayedBody()
    install_provider(monkeypatch, [httpx.Response(200, stream=body)])
    with pytest.raises(HTTPException) as error:
        asyncio.run(invoke(mode, settings(), timeout_seconds=0.01))
    assert error.value.status_code == 504 and body.closed
    assert spans[0]["status"] == "timed_out" and spans[0]["metrics"]["provider_total_tokens"] is None


def test_disconnect_closes_provider_without_claiming_completion(monkeypatch, spans):
    body = DelayedBody(sse([delta("partial")]))
    install_provider(monkeypatch, [httpx.Response(200, stream=body)])
    async def run():
        stream = summary.stream_knowledge_summary(EVENTS, settings())
        assert await anext(stream) == "partial"
        await stream.aclose()
    asyncio.run(run())
    assert body.closed and spans[0]["status"] == "interrupted"
    assert spans[0]["metrics"]["provider_total_tokens"] is None


def test_failed_summary_preserves_record_for_retry_and_caches_only_completion(monkeypatch, spans):
    from app.services import browsing_recording as recording
    monkeypatch.setattr(recording, "admission", lambda _: nullcontext())
    monkeypatch.setattr(recording, "authorize_memory_start", lambda _: True)
    calls, _ = install_provider(monkeypatch, [httpx.Response(200, content=sse([delta("partial")])),
                                             httpx.Response(200, content=sse([terminal()]))])
    owner = Identity("11111111-1111-1111-1111-111111111111")
    store = recording.BrowsingRecordingStore(settings())
    record = recording.Recording(owner_user_id=owner.user_id, ended_at="stopped", events=list(EVENTS))
    store.recordings[(owner.user_id, "record")] = record
    async def run():
        first = [frame async for frame in store.stream_summary("record")]
        assert first[-1][0] == "error" and all(kind != "done" for kind, _ in first)
        assert record.summary is None and record.events == EVENTS and not record.summary_lock.locked()
        second = [frame async for frame in store.stream_summary("record")]
        assert second[-1][0] == "done" and record.summary["summary"] == "回顾正文"
        assert (await store.summarize_recording("record"))["summary"] == "回顾正文"
        assert len(calls) == 2
    with user_context(owner): asyncio.run(run())
    assert [row["status"] for row in spans] == ["failed", "completed"]
