from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.models import AssistantChatMessage, AssistantModuleContext
from app.services import assistant_chat
from app.services.assistant_skills import (
    AssistantSkillContext,
    SkillClarificationRequired,
    SkillExecutionError,
    SkillRegistry,
    build_default_skill_registry,
)
from app.services.assistant_skills.predict_properties import (
    PREDICT_POLYMER_PROPERTIES_SKILL,
    PredictPolymerPropertiesInput,
    build_predict_properties_capability_result,
    format_predict_properties_capability,
)
from app.services.assistant_skills.property_resolver import (
    PropertyResolutionClarification,
    PropertyResolutionError,
    PropertyResolutionUnsupported,
    normalize_prediction_property_arguments,
)


@dataclass(frozen=True)
class AssistantStreamEvent:
    event: str
    payload: dict[str, Any]


def stream_assistant_events(
    *,
    messages: Sequence[AssistantChatMessage],
    modules: Sequence[AssistantModuleContext],
    active_module: str | None,
    api_key: str,
    base_url: str,
    model: str,
    model_enabled: bool,
    model_dir: Path,
) -> Iterable[AssistantStreamEvent]:
    registry = build_default_skill_registry()
    context = AssistantSkillContext(model_enabled=model_enabled, model_dir=model_dir)
    direct_info_skill = _detect_skill_info_request(messages[-1].content)
    if direct_info_skill:
        yield from _stream_skill_info(skill_name=direct_info_skill, registry=registry, context=context)
        return

    decision = assistant_chat.complete_assistant_intent(
        messages=messages,
        modules=modules,
        active_module=active_module,
        skill_catalog=registry.catalog_for_prompt(),
        api_key=api_key,
        base_url=base_url,
        model=model,
    )

    decision_type = str(decision.get("type") or "chat").strip()
    if decision_type == "clarify":
        yield from _emit_text(str(decision.get("message") or "请补充更多信息。"))
        return

    if decision_type == "skill_info":
        skill_name = str(decision.get("skill_name") or PREDICT_POLYMER_PROPERTIES_SKILL).strip()
        yield from _stream_skill_info(skill_name=skill_name, registry=registry, context=context)
        return

    if decision_type != "skill_call":
        yield from _stream_plain_chat(
            messages=messages,
            modules=modules,
            active_module=active_module,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        return

    skill_name = str(decision.get("skill_name") or "").strip()
    skill = registry.get(skill_name)
    if skill is None:
        detail = f"未注册的助手技能：{skill_name or 'unknown'}。"
        yield AssistantStreamEvent("skill_error", {"skill_name": skill_name or "unknown", "detail": detail})
        yield from _emit_text(detail)
        return

    raw_arguments = decision.get("arguments") or {}
    if not isinstance(raw_arguments, dict):
        raw_arguments = {}

    call_id: str | None = None
    try:
        arguments = skill.validate_arguments(raw_arguments)
        if skill.name == PREDICT_POLYMER_PROPERTIES_SKILL:
            if not isinstance(arguments, PredictPolymerPropertiesInput):
                raise SkillExecutionError("invalid prediction skill arguments")
            arguments = normalize_prediction_property_arguments(
                arguments=arguments,
                latest_user_message=messages[-1].content,
                context=context,
                api_key=api_key,
                base_url=base_url,
                model=model,
            )
            raw_arguments = arguments.model_dump()
        call_id = uuid4().hex
        yield AssistantStreamEvent(
            "skill_start",
            {
                "skill_call_id": call_id,
                "skill_name": skill.name,
                "display_name": skill.display_name,
                "arguments": raw_arguments,
            },
        )
        result = skill.execute(arguments, context)
    except SkillClarificationRequired as exc:
        yield from _emit_text(str(exc))
        return
    except PropertyResolutionClarification as exc:
        yield from _emit_text(str(exc))
        return
    except PropertyResolutionUnsupported as exc:
        detail = str(exc)
        yield AssistantStreamEvent("skill_error", {"skill_name": skill.name, "detail": detail})
        yield from _emit_text(detail)
        return
    except PropertyResolutionError as exc:
        detail = str(exc)
        yield AssistantStreamEvent("skill_error", {"skill_name": skill.name, "detail": detail})
        yield from _emit_text(detail)
        return
    except ValidationError as exc:
        clarification = _clarification_for_validation(skill.name, exc)
        if clarification:
            yield from _emit_text(clarification)
            return
        detail = _validation_detail(exc)
        yield AssistantStreamEvent("skill_error", {"skill_name": skill.name, "detail": detail})
        yield from _emit_text(detail)
        return
    except SkillExecutionError as exc:
        detail = str(exc)
        payload: dict[str, Any] = {"skill_name": skill.name, "detail": detail}
        if call_id:
            payload["skill_call_id"] = call_id
        yield AssistantStreamEvent("skill_error", payload)
        yield from _emit_text(detail)
        return

    yield AssistantStreamEvent(
        "skill_result",
        {
            "skill_call_id": call_id,
            "skill_name": skill.name,
            "display_name": skill.display_name,
            "result": result,
        },
    )

    summary_tokens: list[str] = []
    try:
        token_stream = assistant_chat.stream_assistant_skill_summary(
            messages=messages,
            modules=modules,
            active_module=active_module,
            skill_name=skill.name,
            skill_result=result,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        for token in token_stream:
            summary_tokens.append(token)
            yield AssistantStreamEvent("token", {"content": token})
    except assistant_chat.AssistantChatModelError:
        fallback = _fallback_skill_summary(result)
        summary_tokens = [fallback]
        yield AssistantStreamEvent("token", {"content": fallback})

    yield AssistantStreamEvent("done", {"message": "".join(summary_tokens)})


def _stream_skill_info(
    *,
    skill_name: str,
    registry: SkillRegistry,
    context: AssistantSkillContext,
) -> Iterable[AssistantStreamEvent]:
    skill = registry.get(skill_name)
    if skill is None:
        yield from _emit_text(f"未注册的助手技能：{skill_name or 'unknown'}。")
        return

    if skill.name == PREDICT_POLYMER_PROPERTIES_SKILL:
        result = build_predict_properties_capability_result(context)
        yield from _emit_text(format_predict_properties_capability(result))
        return

    yield from _emit_text(f"{skill.display_name} 当前没有可展示的能力清单。")


def _detect_skill_info_request(content: str) -> str | None:
    text = "".join(content.lower().split())
    if not text:
        return None

    asks_prediction_scope = (
        "预测接口" in text
        or "预测模型" in text
        or "预测skill" in text
        or "预测技能" in text
        or "prediction" in text
        or "predict" in text
        or "能预测" in text
        or "可预测" in text
        or "可以预测" in text
        or "支持预测" in text
    )
    asks_properties = (
        "哪些性质" in text
        or "什么性质" in text
        or "哪几种性质" in text
        or "哪些属性" in text
        or "什么属性" in text
        or "支持哪些" in text
        or "支持什么" in text
        or "能力" in text
        or "properties" in text
        or "property" in text
        or "capability" in text
        or "support" in text
        or "available" in text
    )
    if asks_prediction_scope and asks_properties:
        return PREDICT_POLYMER_PROPERTIES_SKILL
    return None


def _stream_plain_chat(
    *,
    messages: Sequence[AssistantChatMessage],
    modules: Sequence[AssistantModuleContext],
    active_module: str | None,
    api_key: str,
    base_url: str,
    model: str,
) -> Iterable[AssistantStreamEvent]:
    full_message: list[str] = []
    for token in assistant_chat.stream_assistant_chat(
        messages=messages,
        modules=modules,
        active_module=active_module,
        api_key=api_key,
        base_url=base_url,
        model=model,
    ):
        full_message.append(token)
        yield AssistantStreamEvent("token", {"content": token})
    yield AssistantStreamEvent("done", {"message": "".join(full_message)})


def _emit_text(message: str) -> Iterable[AssistantStreamEvent]:
    yield AssistantStreamEvent("token", {"content": message})
    yield AssistantStreamEvent("done", {"message": message})


def _clarification_for_validation(skill_name: str, exc: ValidationError) -> str | None:
    if skill_name != PREDICT_POLYMER_PROPERTIES_SKILL:
        return None
    for error in exc.errors():
        if tuple(error.get("loc", ())) == ("smiles",):
            return "请提供需要预测的 SMILES。"
    return None


def _validation_detail(exc: ValidationError) -> str:
    first_error = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(item) for item in first_error.get("loc", []))
    message = first_error.get("msg") or "参数校验失败。"
    return f"技能参数无效{f'（{location}）' if location else ''}：{message}"


def _fallback_skill_summary(result: dict[str, Any]) -> str:
    if result.get("type") == "predict_polymer_properties":
        count = len(result.get("properties") or [])
        elapsed = result.get("query_time_ms")
        elapsed_text = f"，耗时 {float(elapsed):.1f} ms" if isinstance(elapsed, (int, float)) else ""
        return f"已完成性质预测，共返回 {count} 个性质{elapsed_text}。"
    return "已完成技能调用，结构化结果已显示。"
