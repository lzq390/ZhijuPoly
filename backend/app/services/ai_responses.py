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


def completed_responses_text(response: Any) -> str:
    """Validate the same terminal text contract for SDK and raw HTTP callers."""
    if _value(response, "status") != "completed":
        raise AIResponseError("AI provider response did not complete")
    text = _text(response)
    if not text.strip():
        raise AIResponseError("AI provider response was empty")
    return text


class ResponsesTextStream:
    """Content-only state machine; callers own I/O, limits and accounting."""

    def __init__(self):
        self.parts: list[str] = []
        self.completed = False

    def feed(self, event: Any) -> str:
        kind = _value(event, "type", "")
        if self.completed:
            raise AIResponseError("AI provider emitted an event after completion")
        text = ""
        if kind == "response.output_text.delta":
            text = _value(event, "delta", "")
        elif kind == "response.output_text.done" and not self.parts:
            text = _value(event, "text", "")
        elif kind in {"response.failed", "response.incomplete", "error",
                      "response.refusal.delta", "response.refusal.done"}:
            raise AIResponseError("AI provider response failed or was incomplete")
        elif kind == "response.completed":
            text = completed_responses_text(_value(event, "response"))
            if self.parts and text != "".join(self.parts):
                raise AIResponseError("AI provider terminal text differs from stream")
            self.completed = True
            if self.parts:
                text = ""
        if isinstance(text, str) and text:
            self.parts.append(text)
            return text
        return ""

    def finish(self) -> str:
        text = "".join(self.parts)
        if not self.completed or not text.strip():
            raise AIResponseError("AI provider stream ended without a complete text response")
        return text


def complete_responses_text(client, *, model, instructions, messages, max_output_tokens, phase):
    response = provider_call(
        client.responses.create, usage_api="responses", usage_phase=phase,
        model=model, instructions=instructions, input=responses_input(messages),
        max_output_tokens=max_output_tokens, reasoning={"effort": "medium"}, store=False,
    )
    return completed_responses_text(response)


def stream_responses_text(client, *, model, instructions, messages, max_output_tokens, phase):
    stream = provider_call(
        client.responses.create, usage_api="responses", usage_phase=phase,
        model=model, instructions=instructions, input=responses_input(messages),
        max_output_tokens=max_output_tokens, reasoning={"effort": "medium"}, store=False, stream=True,
    )
    text_stream = ResponsesTextStream()
    try:
        for event in stream:
            text = text_stream.feed(event)
            if text:
                yield text
        text_stream.finish()
    except AIResponseError as error:
        # The raw terminal event is observed before this content contract can
        # validate it. A rejected/malformed stream must not retain that earlier
        # optimistic provider-success outcome when it is closed.
        close_provider_stream(stream, error=error)
        raise
    finally:
        close_provider_stream(stream)
