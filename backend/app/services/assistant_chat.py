from __future__ import annotations

from collections.abc import Iterable, Sequence
from urllib.parse import urlparse

from openai import OpenAI

from app.models import AssistantChatMessage, AssistantModuleContext


class AssistantChatConfigError(RuntimeError):
    pass


class AssistantChatModelError(RuntimeError):
    pass


def validate_assistant_model_access(*, api_key: str, base_url: str, model: str) -> None:
    if not api_key:
        raise AssistantChatConfigError("Assistant API Key is required")
    if not model:
        raise AssistantChatConfigError("Assistant model is required")
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.hostname:
        raise AssistantChatConfigError("Assistant Base URL must include protocol and host")


def stream_assistant_chat(
    *,
    messages: Sequence[AssistantChatMessage],
    modules: Sequence[AssistantModuleContext],
    active_module: str | None,
    api_key: str,
    base_url: str,
    model: str,
) -> Iterable[str]:
    validate_assistant_model_access(api_key=api_key, base_url=base_url, model=model)
    client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))

    try:
        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": build_assistant_system_prompt(modules, active_module)},
                *[{"role": message.role, "content": message.content} for message in messages],
            ],
            temperature=0.2,
            max_tokens=1400,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = getattr(chunk.choices[0], "delta", None)
            content = getattr(delta, "content", None) if delta is not None else None
            if content:
                yield content
    except AssistantChatConfigError:
        raise
    except Exception as exc:
        raise AssistantChatModelError(str(exc)) from exc


def build_assistant_system_prompt(
    modules: Sequence[AssistantModuleContext],
    active_module: str | None,
) -> str:
    module_lines = "\n".join(
        (
            f"- id: {module.id}; display: {module.title}; group: {module.group}; "
            f"clickable marker: [[module:{module.id}|{module.title}]]; "
            f"description: {module.description}"
        )
        for module in modules
    )
    active_module_text = active_module or "home"
    return f"""You are 智聚万物, the ZhijuPoly polymer research assistant.

Your role:
- Help researchers reason about polymer data, structure-property exploration, knowledge retrieval, and polymer design.
- Give concise, scientifically grounded answers in the user language.
- Never expose internal route paths, URL paths, route names, or strings beginning with "/" to the user.
- When useful, recommend one of the available ZhijuPoly modules by using the exact clickable marker from the module list, for example [[module:labData|实验数据采集]]. Do not wrap module markers in Markdown.
- Do not claim that you have executed a module, query, prediction, search, or design job. You can guide the user to the right module or explain what to do there.
- If the user asks for unavailable automation, clearly say that this v1 assistant can advise and navigate, but cannot execute tools on behalf of the user.

Current active module: {active_module_text}

Available modules:
{module_lines or "- No module context was provided."}
""".strip()
