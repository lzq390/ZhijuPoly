"""Content-free accounting at actual provider call boundaries.

SDK retries remain disabled. Counters are absolute observations from providers,
never estimates, sums of chunks, request limits, or character counts.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any

from app.usage_context import current_usage_context, usage_span


_TOKEN_FIELDS = (
    "provider_input_tokens", "provider_output_tokens", "provider_total_tokens",
    "cached_input_tokens", "reasoning_tokens",
)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _count(value: Any) -> int | None:
    # bool, numeric strings, negatives and non-finite values are not usage.
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def provider_usage(response: Any, *, api: str = "chat") -> dict[str, int | None]:
    usage = _value(response, "usage")
    input_name, output_name = (("input_tokens", "output_tokens") if api == "responses"
                               else ("prompt_tokens", "completion_tokens"))
    return {
        "provider_input_tokens": _count(_value(usage, input_name)),
        "provider_output_tokens": _count(_value(usage, output_name)),
        "provider_total_tokens": _count(_value(usage, "total_tokens")),
        "cached_input_tokens": _count(_value(_value(usage, input_name + "_details"), "cached_tokens")),
        "reasoning_tokens": _count(_value(_value(usage, output_name + "_details"), "reasoning_tokens")),
    }


def _failure_status(error: BaseException) -> str:
    # Inspect only types/status, never log or persist raw exception messages.
    if isinstance(error, (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt)):
        return "interrupted"
    if isinstance(error, TimeoutError) or "timeout" in type(error).__name__.lower():
        return "timed_out"
    if getattr(error, "status_code", None) in (408, 504):
        return "timed_out"
    return "failed"


class ProviderAttempt:
    """One external invocation, safe to retain across generator yields/threads."""

    def __init__(self) -> None:
        context = current_usage_context()
        self._span = usage_span(context.module if context else "provider", kind="provider")
        self._started = monotonic()
        self._metrics: dict[str, int | None] = dict.fromkeys(_TOKEN_FIELDS)
        self._outcome: str | None = None
        self._finished = False

    def start(self) -> "ProviderAttempt":
        # No ContextVar binding survives a yield or a cross-thread close.
        self._span.start(bind=False)
        self._started = monotonic()
        return self

    def observe(self, response: Any, *, api: str = "chat") -> None:
        for key, value in provider_usage(response, api=api).items():
            if value is not None:
                self._metrics[key] = value

    def terminal(self, status: str = "completed") -> None:
        self._outcome = status

    def finish(self, status: str | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        self._metrics["observed_wall_ms"] = max(0, int((monotonic() - self._started) * 1000))
        self._span.finish(status=self._outcome or status or "interrupted", metrics=self._metrics)

    def __enter__(self) -> "ProviderAttempt":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.finish(_failure_status(exc) if exc is not None else "interrupted")
        return False


class _ProviderStream:
    def __init__(self, stream: Any, attempt: ProviderAttempt, *, api: str) -> None:
        self._stream = stream
        self._iterator = iter(stream)
        self._attempt = attempt
        self._api = api
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        try:
            event = next(self._iterator)
        except StopIteration:
            self.close()
            raise
        except BaseException as error:
            if self._attempt._outcome is None:
                self._attempt.terminal(_failure_status(error))
            raise
        if self._api == "responses":
            event_type = _value(event, "type", "")
            if event_type in {"response.completed", "response.failed", "response.incomplete"}:
                self._attempt.observe(_value(event, "response"), api="responses")
                self._attempt.terminal("completed" if event_type == "response.completed" else "failed")
            elif event_type == "error":
                self._attempt.terminal("failed")
        else:
            # Observe even the last usage-only chunk with choices=[].
            self._attempt.observe(event)
            if any(_value(choice, "finish_reason") is not None for choice in (_value(event, "choices", []) or [])):
                self._attempt.terminal()
        return event

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._stream, "close", None)
            if close is not None:
                close()
        except BaseException as error:
            self._attempt.finish(_failure_status(error))
            raise
        finally:
            self._attempt.finish()


def provider_call(create: Callable[..., Any], /, *, usage_api: str = "chat", **kwargs: Any) -> Any:
    """Call exactly once; never retry when usage or stream options are absent.

Do not inject include_usage for arbitrary compatible endpoints: some reject it.
Existing request options are preserved; absent provider counters remain null.
"""
    attempt = ProviderAttempt().start()
    try:
        response = create(**kwargs)
        if kwargs.get("stream"):
            return _ProviderStream(response, attempt, api=usage_api)
        attempt.observe(response, api=usage_api)
        attempt.terminal()
        attempt.finish()
        return response
    except BaseException as error:
        attempt.finish(_failure_status(error))
        raise


def close_provider_stream(stream: Any) -> None:
    if isinstance(stream, _ProviderStream):
        stream.close()


def provider_http_request(request: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Observe one public literature HTTP invocation, without storing its URL/body."""
    with ProviderAttempt() as attempt:
        response = request(*args, **kwargs)
        attempt.terminal("failed" if response.status_code >= 400 else "completed")
        return response
