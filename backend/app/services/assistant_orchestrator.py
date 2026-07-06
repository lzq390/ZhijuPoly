from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
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
from app.services.smiles_to_iupac import IupacNameLookupAmbiguousError, IupacSmilesMatch


IupacMatchFinder = Callable[[str], list[IupacSmilesMatch]]


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
    iupac_match_finder: IupacMatchFinder | None = None,
) -> Iterable[AssistantStreamEvent]:
    normalized_messages, input_clarification = _normalize_iupac_structure_input(
        messages=messages,
        iupac_match_finder=iupac_match_finder,
    )
    if input_clarification:
        yield from _emit_text(input_clarification)
        return
    messages = normalized_messages

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
    if skill.name == PREDICT_POLYMER_PROPERTIES_SKILL:
        summary = _format_predict_properties_summary(result, messages[-1].content)
        summary_tokens = [summary]
        yield AssistantStreamEvent("token", {"content": summary})
    else:
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


def _normalize_iupac_structure_input(
    *,
    messages: Sequence[AssistantChatMessage],
    iupac_match_finder: IupacMatchFinder | None = None,
) -> tuple[list[AssistantChatMessage], str | None]:
    normalized_messages = list(messages)
    if not normalized_messages:
        return normalized_messages, None

    latest_message = normalized_messages[-1]
    if _has_resolved_structure_input(latest_message.content):
        return normalized_messages, None
    if iupac_match_finder is None:
        return normalized_messages, None
    if not _should_lookup_iupac_name(latest_message.content):
        return normalized_messages, None

    try:
        matches = iupac_match_finder(latest_message.content)
    except IupacNameLookupAmbiguousError as exc:
        return normalized_messages, f"IUPAC 名称解析存在歧义：{exc}。请提供对应 SMILES。"
    except Exception as exc:
        return normalized_messages, f"IUPAC 缓存读取失败：{exc}。请提供对应 SMILES。"

    if len(matches) > 1:
        names = "、".join(match.iupac_name for match in matches)
        return normalized_messages, f"识别到多个 IUPAC 名称：{names}。请指定要操作的一个 IUPAC 名称。"

    if not matches:
        return normalized_messages, _clarification_for_unmatched_iupac(latest_message.content)

    enhanced_message = latest_message.model_copy(
        update={"content": _append_resolved_iupac_context(latest_message.content, matches[0])}
    )
    normalized_messages[-1] = enhanced_message
    return normalized_messages, None


def _clarification_for_unmatched_iupac(content: str) -> str | None:
    if _looks_like_missing_iupac_name(content):
        return "请提供需要操作的 IUPAC 名称或 SMILES。"
    if _looks_like_unresolved_iupac_request(content):
        return "当前 IUPAC 缓存中没有找到该名称，请提供对应 SMILES 或确认名称完全一致。"
    return None


def _append_resolved_iupac_context(content: str, match: IupacSmilesMatch) -> str:
    return (
        f"{content}\n\n"
        "[Resolved structure input]\n"
        f"Original IUPAC name: {match.iupac_name}\n"
        f"Resolved SMILES: {match.smiles}\n"
        "Resolved SMILES source: smiles_iupac_cache\n"
        "Use the resolved SMILES as the structure input for any downstream task."
    )


def _has_resolved_structure_input(content: str) -> bool:
    return "[Resolved structure input]" in content and "Resolved SMILES:" in content


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


def _looks_like_missing_iupac_name(content: str) -> bool:
    text = content.casefold()
    return "iupac" in text and _looks_like_structure_task(text) and not _looks_like_iupac_name_text(text)


def _looks_like_unresolved_iupac_request(content: str) -> bool:
    text = content.casefold()
    return _looks_like_structure_task(text) and _looks_like_iupac_name_text(text)


def _should_lookup_iupac_name(content: str) -> bool:
    return _looks_like_missing_iupac_name(content) or _looks_like_unresolved_iupac_request(content)


def _looks_like_structure_task(text: str) -> bool:
    return any(
        keyword in text
        for keyword in (
            "预测",
            "估算",
            "计算",
            "性质",
            "属性",
            "predict",
            "prediction",
            "estimate",
            "property",
            "properties",
            "smiles",
            "结构",
            "3d",
            "画板",
            "查询",
            "相似",
        )
    )


def _looks_like_iupac_name_text(text: str) -> bool:
    if not any(term in text for term in ("-", "(", ")", ",")):
        return False
    return any(
        term in text
        for term in (
            "acrylonitrile",
            "phenyl",
            "pyrimidin",
            "pyridyl",
            "benzene",
            "amine",
            "amino",
            "methyl",
            "ethyl",
            "isopropenyl",
            "prop-",
        )
    )


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
            return "请提供需要预测的 SMILES 或 IUPAC 名称。"
    return None


def _validation_detail(exc: ValidationError) -> str:
    first_error = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(item) for item in first_error.get("loc", []))
    message = first_error.get("msg") or "参数校验失败。"
    return f"技能参数无效{f'（{location}）' if location else ''}：{message}"


def _fallback_skill_summary(result: dict[str, Any]) -> str:
    if result.get("type") == "predict_polymer_properties":
        count = len(result.get("properties") or [])
        return f"已完成性质预测，共返回 {count} 个性质。"
    return "已完成技能调用，结构化结果已显示。"


def _format_predict_properties_summary(result: dict[str, Any], latest_user_message: str) -> str:
    smiles = str(result.get("smiles") or "").strip()
    resolved_source = _extract_resolved_structure_source(latest_user_message)
    if resolved_source == "molscribe_image_recognition":
        original_image_file = _extract_original_image_file(latest_user_message)
        heading = (
            f"对图片 **{original_image_file}** 识别得到的 SMILES："
            if original_image_file
            else "图片识别得到的 SMILES："
        )
    elif original_iupac_name := _extract_original_iupac_name(latest_user_message):
        heading = f"对 **{original_iupac_name}** 的解析 SMILES："
    else:
        heading = "已解析 SMILES："
    return "\n\n".join((heading, f"`{smiles}`", "预测结果如下："))


def _extract_line_value(content: str, prefix: str) -> str | None:
    for line in content.splitlines():
        if line.startswith(prefix):
            value = line.removeprefix(prefix).strip()
            return value or None
    return None


def _extract_original_iupac_name(content: str) -> str | None:
    return _extract_line_value(content, "Original IUPAC name:")


def _extract_original_image_file(content: str) -> str | None:
    return _extract_line_value(content, "Original image file:")


def _extract_resolved_structure_source(content: str) -> str | None:
    return _extract_line_value(content, "Resolved SMILES source:")
