from __future__ import annotations

from typing import Any

from openai import DefaultHttpxClient, OpenAI


_CLEAN_PROVIDER_ERRORS = frozenset(
    {
        "AI provider authentication or access was rejected.",
        "AI provider capacity or quota is temporarily unavailable.",
        "AI provider request timed out.",
        "AI provider is temporarily unavailable.",
        "AI provider network connection failed.",
        "AI provider request failed.",
    }
)


def create_openai_client(
    *,
    api_key: str,
    base_url: str,
    proxy_url: str = "",
    timeout_seconds: float = 90.0,
) -> OpenAI:
    """Create an OpenAI-compatible client without inheriting process proxy state."""

    http_options: dict[str, Any] = {"trust_env": False}
    if proxy_url:
        http_options["proxy"] = proxy_url
    return OpenAI(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        timeout=timeout_seconds,
        max_retries=0,
        http_client=DefaultHttpxClient(**http_options),
    )


def clean_ai_provider_error(error: BaseException) -> str:
    """Map provider failures to stable text without leaking URLs or response bodies."""

    normalized_message = " ".join(str(error).split())
    if normalized_message in _CLEAN_PROVIDER_ERRORS:
        return normalized_message
    status_code = getattr(error, "status_code", None)
    name = type(error).__name__.casefold()
    if status_code in {401, 403} or "authentication" in name or "permission" in name:
        return "AI provider authentication or access was rejected."
    if status_code == 429 or "ratelimit" in name or "rate_limit" in name:
        return "AI provider capacity or quota is temporarily unavailable."
    if status_code in {408, 504} or "timeout" in name:
        return "AI provider request timed out."
    if isinstance(status_code, int) and status_code >= 500:
        return "AI provider is temporarily unavailable."
    if "connection" in name or "network" in name:
        return "AI provider network connection failed."
    return "AI provider request failed."
