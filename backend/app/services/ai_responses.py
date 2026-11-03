"""Text Responses transport with real provider accounting and terminal checks."""
from __future__ import annotations

from typing import Any

from app.services.usage_provider import close_provider_stream, provider_call


class AIResponseError(RuntimeError):
    pass


def _value(item: Any, key: str, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def responses_input(messages: list[dict]) -> list[dict]:
    result = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            converted = []
            for part in content:
                if part["type"] == "text":
                    converted.append({"type": "input_text", "text": part["text"]})
                elif part["type"] == "image_url":
                    converted.append({"type": "input_image", "image_url": part["image_url"]["url"]})
                else:
                    raise AIResponseError("Unsupported Responses input part")
            content = converted
        result.append({"role": message["role"], "content": content})
    return result


def _text(response: Any) -> str:
    output = _value(response, "output", []) or []
    parts = [part for item in output for part in (_value(item, "content", []) or [])]
    if any(_value(part, "type") == "refusal" for part in parts):
        raise AIResponseError("AI provider refused the response")
    text = _value(response, "output_text")
    if not isinstance(text, str) or not text:
        text = "".join(_value(part, "text", "") for part in parts
                       if _value(part, "type") == "output_text" and isinstance(_value(part, "text"), str))
    return text


def complete_responses_text(client, *, model, instructions, messages, max_output_tokens, phase):
    response = provider_call(
        client.responses.create, usage_api="responses", usage_phase=phase,
        model=model, instructions=instructions, input=responses_input(messages),
        max_output_tokens=max_output_tokens, reasoning={"effort": "medium"}, store=False,
    )
    if _value(response, "status") != "completed":
        raise AIResponseError("AI provider response did not complete")
    text = _text(response)
    if not text.strip():
        raise AIResponseError("AI provider response was empty")
    return text


def stream_responses_text(client, *, model, instructions, messages, max_output_tokens, phase):
    stream = provider_call(
        client.responses.create, usage_api="responses", usage_phase=phase,
        model=model, instructions=instructions, input=responses_input(messages),
        max_output_tokens=max_output_tokens, reasoning={"effort": "medium"}, store=False, stream=True,
    )
    parts = []
    completed = False
    try:
        for event in stream:
            kind = _value(event, "type", "")
            if completed:
                raise AIResponseError("AI provider emitted an event after completion")
            if kind == "response.output_text.delta":
                delta = _value(event, "delta", "")
                if isinstance(delta, str) and delta:
                    parts.append(delta)
                    yield delta
            elif kind == "response.output_text.done" and not parts:
                text = _value(event, "text", "")
                if isinstance(text, str) and text:
                    parts.append(text)
                    yield text
            elif kind in {"response.failed", "response.incomplete", "error",
                          "response.refusal.delta", "response.refusal.done"}:
                raise AIResponseError("AI provider response failed or was incomplete")
            elif kind == "response.completed":
                response = _value(event, "response")
                if _value(response, "status") != "completed":
                    raise AIResponseError("AI provider response did not complete")
                text = _text(response)
                if not text.strip():
                    raise AIResponseError("AI provider terminal response was empty")
                if parts and text != "".join(parts):
                    raise AIResponseError("AI provider terminal text differs from stream")
                completed = True
                if not parts and text:
                    parts.append(text)
                    yield text
        if not completed or not "".join(parts).strip():
            raise AIResponseError("AI provider stream ended without a complete text response")
    except AIResponseError as error:
        # The raw terminal event is observed before this content contract can
        # validate it. A rejected/malformed stream must not retain that earlier
        # optimistic provider-success outcome when it is closed.
        close_provider_stream(stream, error=error)
        raise
    finally:
        close_provider_stream(stream)
