"""Provider mocks only: no network, database, GPU, or external inference."""
import json
import socket
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from app.models import AssistantChatMessage
from app.services import ai_responses as transport, assistant_chat as chat, usage_provider as usage
from app.services import assistant_orchestrator as orchestrator
from app.services.assistant_completion import CompletionEvidence, completion_scope
from app.services.online_knowledge.extractor import PolymerExtractor


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Test attempted real network access")
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def spans(monkeypatch):
    rows = []
    class Span:
        def start(self, **_):
            pass
        def finish(self, *, status, metrics):
            rows.append({"status": status, "metrics": metrics})
    monkeypatch.setattr(usage, "usage_span", lambda *_, **__: Span())
    return rows


def response(text="OK", status="completed", **kwargs):
    return NS(status=status, output_text=text, output=[], usage=NS(
        input_tokens=11, output_tokens=3, total_tokens=14,
        input_tokens_details=NS(cached_tokens=2), output_tokens_details=NS(reasoning_tokens=1),
    ), **kwargs)


def events(text="OK"):
    return [NS(type="response.output_text.delta", delta=text),
            NS(type="response.completed", response=response(text))]


def client_with(outputs):
    calls, closed = [], []
    def create(**kwargs):
        calls.append(kwargs)
        value = outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return iter(value) if kwargs.get("stream") else value
    def forbidden(**_):
        raise AssertionError("Responses configuration called Chat Completions")
    return NS(responses=NS(create=create), chat=NS(completions=NS(create=forbidden)),
              close=lambda: closed.append(True)), calls, closed


def common():
    return dict(messages=[AssistantChatMessage(role="user", content="Private prompt")], modules=[],
                active_module=None, api_key="private-key", base_url="https://unused.invalid/v1",
                model="configured-model", transport="responses")


@pytest.mark.parametrize("case,cap,phase", [
    ("intent", 1600, "intent"), ("answer", 2400, "answer"), ("image", 2400, "answer"),
    ("summary", 1600, "answer"), ("resolution", 1600, "resolution"),
])
def test_all_five_general_calls_use_real_responses_and_usage(monkeypatch, spans, case, cap, phase):
    text = '{"type":"chat"}' if case in {"intent", "resolution"} else "OK"
    client, calls, closed = client_with([response(text) if case in {"intent", "resolution"} else events(text)])
    monkeypatch.setattr(chat, "create_openai_client", lambda **_: client)
    evidence = CompletionEvidence(4, 4000)
    args = common()
    with completion_scope(evidence):
        if case == "intent":
            result = chat.complete_assistant_intent(**args, skill_catalog="fixture")
            assert result == {"type": "chat"}
        elif case == "answer":
            assert "".join(chat.stream_assistant_chat(**args)) == text
        elif case == "image":
            assert "".join(chat.stream_assistant_image_chat(**args, image_bytes=b"fixture", content_type="image/png")) == text
            parts = calls[0]["input"][-1]["content"]
            assert parts == [{"type": "input_text", "text": "Private prompt"},
                             {"type": "input_image", "image_url": "data:image/png;base64,Zml4dHVyZQ=="}]
        elif case == "summary":
            assert "".join(chat.stream_assistant_skill_summary(**args, skill_name="fixture", skill_result={"value": 1})) == text
            assert "Skill execution result JSON" in calls[0]["input"][-1]["content"]
        else:
            for key in ("messages", "modules", "active_module"):
                args.pop(key)
            assert chat.complete_prediction_property_resolution(**args, latest_user_message="density",
                requested_properties=["density"], property_catalog=[]) == {"type": "chat"}
    assert len(calls) == len(closed) == len(spans) == 1
    assert calls[0]["max_output_tokens"] == cap
    assert calls[0]["store"] is False
    assert calls[0]["reasoning"] == {"effort": "medium"}
    assert not set(calls[0]) & {"max_tokens", "max_completion_tokens", "messages", "temperature", "tools"}
    assert evidence._phases[phase]["completed"] is True
    assert spans[0]["status"] == "completed"
    assert spans[0]["metrics"]["provider_input_tokens"] == 11
    assert spans[0]["metrics"]["provider_output_tokens"] == 3
    assert spans[0]["metrics"]["cached_input_tokens"] == 2
    assert "Private prompt" not in repr(spans)


@pytest.mark.parametrize("status", ["incomplete", "failed", "in_progress", None])
def test_http_success_without_completed_status_fails_sync(spans, status):
    client, calls, _ = client_with([response(status=status)])
    with pytest.raises(transport.AIResponseError, match="did not complete"):
        transport.complete_responses_text(client, model="m", instructions="i", messages=[], max_output_tokens=10, phase="intent")
    assert len(calls) == 1
    assert spans[0]["status"] == "failed"
    assert spans[0]["metrics"]["provider_total_tokens"] == 14


@pytest.mark.parametrize("terminal", ["response.failed", "response.incomplete", "error",
                                       "response.refusal.delta", "response.refusal.done", "missing"])
def test_partial_text_never_succeeds_without_real_completed_event(spans, terminal):
    values = [NS(type="response.output_text.delta", delta="partial")]
    if terminal != "missing":
        values.append(NS(type=terminal, response=response("partial", status="failed")))
    client, calls, _ = client_with([values])
    with pytest.raises(transport.AIResponseError):
        list(transport.stream_responses_text(client, model="m", instructions="i", messages=[], max_output_tokens=10, phase="answer"))
    assert len(calls) == 1
    assert spans[0]["status"] != "completed"


@pytest.mark.parametrize("values", [
    [NS(type="response.completed", response=response(""))],
    [NS(type="response.output_text.delta", delta="OK"), NS(type="response.completed")],
    [NS(type="response.output_text.delta", delta="OK"), NS(type="response.completed", response=response("OK", status=None))],
    [NS(type="response.output_text.delta", delta="OK"), NS(type="response.completed", response=response(""))],
    [NS(type="response.output_text.delta", delta="OK"), NS(type="response.completed", response=response("different"))],
    [NS(type="response.completed", response=response("OK", status="failed"))],
])
def test_empty_or_inconsistent_terminal_response_is_rejected(spans, values):
    client, _, _ = client_with([values])
    with pytest.raises(transport.AIResponseError):
        list(transport.stream_responses_text(client, model="m", instructions="i", messages=[], max_output_tokens=10, phase="answer"))
    assert len(spans) == 1 and spans[0]["status"] == "failed"


@pytest.mark.parametrize("late", [
    NS(type="response.output_text.delta", delta="must not be emitted"),
    NS(type="response.output_text.done", text="must not be emitted"),
    NS(type="response.completed", response=response("second terminal")),
    NS(type="response.refusal.done"),
])
def test_post_terminal_event_is_rejected_without_emitting_more_text(spans, late):
    closed = []

    def raw():
        try:
            yield from [*events("OK"), late]
        finally:
            closed.append(True)

    client = NS(responses=NS(create=lambda **_: raw()))
    stream = transport.stream_responses_text(client, model="m", instructions="i", messages=[],
                                               max_output_tokens=10, phase="answer")
    output = []
    with pytest.raises(transport.AIResponseError, match="after completion"):
        for text in stream:
            output.append(text)
    assert output == ["OK"] and closed == [True]
    assert len(spans) == 1 and spans[0]["status"] == "failed"
    assert spans[0]["metrics"]["provider_total_tokens"] == 14


def test_sdk_output_only_and_output_text_done_fallback_do_not_duplicate(spans):
    client, calls, _ = client_with([[NS(type="response.output_text.done", text="OK"),
                                   NS(type="response.completed", response=response("OK"))]])
    assert list(transport.stream_responses_text(client, model="m", instructions="i", messages=[], max_output_tokens=10, phase="answer")) == ["OK"]
    client, _, _ = client_with([[NS(type="response.completed", response=response("OK"))]])
    assert list(transport.stream_responses_text(client, model="m", instructions="i", messages=[], max_output_tokens=10, phase="answer")) == ["OK"]


def test_early_close_retains_interrupted_provider_usage(spans):
    closed = []
    def raw():
        try:
            yield NS(type="response.output_text.delta", delta="partial")
            yield NS(type="response.completed", response=response("partial"))
        finally:
            closed.append(True)
    client = NS(responses=NS(create=lambda **_: raw()))
    stream = transport.stream_responses_text(client, model="m", instructions="i", messages=[], max_output_tokens=10, phase="answer")
    assert next(stream) == "partial"
    stream.close()
    assert closed == [True]
    assert spans[0]["status"] == "interrupted"
    assert spans[0]["metrics"]["provider_total_tokens"] is None


def test_general_orchestrator_carries_responses_to_intent_and_answer(monkeypatch, spans):
    client, calls, closed = client_with([response('{"type":"chat"}'), events()])
    monkeypatch.setattr(chat, "create_openai_client", lambda **_: client)
    args = common()
    values = list(orchestrator.stream_assistant_events.__wrapped__(**args, model_enabled=False, model_dir=Path("/unused")))
    assert values[-1].event == "done" and values[-1].payload == {"message": "OK"}
    assert len(calls) == len(closed) == len(spans) == 2


@pytest.mark.parametrize("mode,payload", [
    ("synthesis", {"has_polymerization": False, "total_reactions": 0, "reactions": []}),
    ("property", {"has_polymer": False, "total_data_points": 0, "data_points": []}),
])
def test_online_responses_keeps_strict_scientific_validation(monkeypatch, spans, mode, payload):
    from app.services.online_knowledge import extractor
    client, calls, closed = client_with([response(json.dumps(payload))])
    monkeypatch.setattr(extractor, "create_openai_client", lambda **_: client)
    instance = PolymerExtractor("fake", "https://unused.invalid", "m", transport="responses")
    try:
        result = instance.extract_from_abstract("Title", "Abstract", "doi", mode)
    finally:
        instance.close()
    assert result == {**payload, "paper_id": "doi", "title": "Title"}
    assert calls[0]["max_output_tokens"] == 4000
    assert len(closed) == len(spans) == 1


@pytest.mark.parametrize("raw", ["{}", "not JSON", '{"has_polymerization":true,"total_reactions":1,"reactions":[]}'])
def test_online_responses_invalid_science_never_becomes_success(monkeypatch, spans, raw):
    from app.services.online_knowledge import extractor
    client, _, _ = client_with([response(raw)])
    monkeypatch.setattr(extractor, "create_openai_client", lambda **_: client)
    instance = PolymerExtractor("fake", "https://unused.invalid", "m", transport="responses")
    assert instance.extract_from_abstract("T", "A", "doi", "synthesis")["error"] == "模型返回的提取结果格式无效"
    instance.close()
    # Completed inference remains a completed provider span even if its science is invalid.
    assert spans[0]["status"] == "completed"
