"""Explicit assistant business outcomes; all providers and skills are mocked."""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from pydantic import BaseModel, ValidationError

from app.models import AssistantChatMessage
from app.services import assistant_orchestrator as orchestrator, tg_assistant as tg


def assistant_args():
    return dict(messages=[AssistantChatMessage(role="user", content="hello")], modules=[], active_module=None,
                api_key="fake", base_url="https://unused.invalid", model="fake", model_enabled=False,
                model_dir=Path("/nonexistent"))


def arrange_skill(monkeypatch, *, error=None, missing=False, fail_summary=False):
    marked = []
    monkeypatch.setattr(orchestrator, "mark_current_usage_outcome", marked.append)
    monkeypatch.setattr(orchestrator, "_detect_skill_info_request", lambda _: None)
    monkeypatch.setattr(orchestrator.assistant_chat, "complete_assistant_intent",
                        lambda **_: {"type": "skill_call", "skill_name": "custom", "arguments": {}})
    def validate(_):
        if error is not None:
            raise error
        return {}
    skill = NS(name="custom", display_name="custom", validate_arguments=validate, execute=lambda *_: {"value": 1})
    registry = NS(catalog_for_prompt=lambda: "", get=lambda _: None if missing else skill)
    monkeypatch.setattr(orchestrator, "build_default_skill_registry", lambda: registry)
    if fail_summary:
        def summary(**_):
            raise orchestrator.assistant_chat.AssistantChatModelError("mock")
            yield
        monkeypatch.setattr(orchestrator.assistant_chat, "stream_assistant_skill_summary", summary)
    return marked


def validation_error():
    class Arguments(BaseModel):
        required_number: int
    try:
        Arguments(required_number="invalid")
    except ValidationError as error:
        return error


@pytest.mark.parametrize("failure", ["missing", "unsupported", "resolution", "validation", "execution"])
def test_structured_skill_error_marks_failed_even_with_done(monkeypatch, failure):
    errors = {"unsupported": orchestrator.PropertyResolutionUnsupported("mock"),
              "resolution": orchestrator.PropertyResolutionError("mock"),
              "validation": validation_error(), "execution": orchestrator.SkillExecutionError("mock")}
    marked = arrange_skill(monkeypatch, error=errors.get(failure), missing=failure == "missing")
    events = list(orchestrator.stream_assistant_events.__wrapped__(**assistant_args()))
    assert any(event.event == "skill_error" for event in events)
    assert events[-1].event == "done"
    assert marked == ["failed"]


@pytest.mark.parametrize("error", [orchestrator.SkillClarificationRequired("more info"),
                                    orchestrator.PropertyResolutionClarification("more info")])
def test_clarification_is_a_valid_business_response(monkeypatch, error):
    marked = arrange_skill(monkeypatch, error=error)
    events = list(orchestrator.stream_assistant_events.__wrapped__(**assistant_args()))
    assert events[-1].event == "done"
    assert marked == []


def test_successful_summary_fallback_does_not_fail_root(monkeypatch):
    marked = arrange_skill(monkeypatch, fail_summary=True)
    events = list(orchestrator.stream_assistant_events.__wrapped__(**assistant_args()))
    assert any(event.event == "skill_result" for event in events)
    assert events[-1].event == "done"
    assert marked == []


def tg_args():
    return dict(messages=[AssistantChatMessage(role="user", content="hello")], context_payload=None,
                api_key="fake", base_url="https://unused.invalid", model="fake", proxy_url="",
                image_inputs=(), reasoning_effort="medium", cancelled=lambda: True)


@pytest.mark.parametrize("phase", ["intent", "answer"])
def test_responses_disconnect_exit_marks_interrupted(monkeypatch, phase):
    marked, closed = [], []
    monkeypatch.setattr(tg, "mark_current_usage_outcome", marked.append)
    client = NS(responses=NS(create=lambda **_: iter([NS(type="response.output_text.delta", delta="x")])),
                close=lambda: closed.append(True))
    monkeypatch.setattr(tg, "create_openai_client", lambda **_: client)
    function = tg._stream_responses_intent if phase == "intent" else tg._stream_responses_answer
    list(function(**tg_args()))
    assert marked == ["interrupted"]
    assert closed == [True]


def test_chat_disconnect_exit_marks_interrupted(monkeypatch):
    marked = []
    monkeypatch.setattr(tg, "mark_current_usage_outcome", marked.append)
    monkeypatch.setattr(tg, "_stream_answer", lambda **_: iter(["partial"]))
    list(tg._stream_model_answer(**tg_args(), transport="chat_completions"))
    assert marked == ["interrupted"]


@pytest.mark.parametrize("cancel_checks", [[True], [False, True]])
def test_top_level_cancellation_never_claims_done(monkeypatch, cancel_checks):
    marked = []
    monkeypatch.setattr(tg, "mark_current_usage_outcome", marked.append)
    def intent(**_):
        yield from ()
        return {"type": "chat"}
    def answer(**_):
        yield from ()
        return "answer"
    monkeypatch.setattr(tg, "_stream_intent", intent)
    monkeypatch.setattr(tg, "_stream_model_answer", answer)
    checks = iter(cancel_checks)
    events = list(tg.stream_tg_assistant_events.__wrapped__(
        messages=[AssistantChatMessage(role="user", content="hello")], page_context=None,
        api_key="fake", base_url="https://unused.invalid", model="fake", cancelled=lambda: next(checks)))
    assert not any(event.event == "done" for event in events)
    assert marked == ["interrupted"]
