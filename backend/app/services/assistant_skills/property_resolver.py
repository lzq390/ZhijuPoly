from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.services import assistant_chat
from app.services.assistant_skills.predict_properties import (
    PredictPolymerPropertiesInput,
    build_predict_properties_capability_result,
    resolve_property_names,
)
from app.services.assistant_skills.registry import AssistantSkillContext
from app.utils.exceptions import UnsupportedPredictionPropertyError


class PropertyResolutionError(RuntimeError):
    pass


class PropertyResolutionClarification(RuntimeError):
    pass


class PropertyResolutionUnsupported(RuntimeError):
    pass


class ResolvedPredictionProperties(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["resolved"]
    properties: list[str] = Field(min_length=1)

    @field_validator("properties")
    @classmethod
    def normalize_properties(cls, properties: list[str]) -> list[str]:
        normalized = [value.strip() for value in properties]
        if any(not value for value in normalized):
            raise ValueError("resolved property names must not be empty")
        return normalized


class UnsupportedPredictionProperties(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["unsupported"]
    requested: list[str] = Field(min_length=1)
    message: str = Field(min_length=1)

    @field_validator("requested")
    @classmethod
    def normalize_requested(cls, requested: list[str]) -> list[str]:
        normalized = [value.strip() for value in requested]
        if any(not value for value in normalized):
            raise ValueError("unsupported property names must not be empty")
        return normalized


class ClarifyPredictionProperties(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["clarify"]
    message: str = Field(min_length=1)


PropertyResolution = ResolvedPredictionProperties | UnsupportedPredictionProperties | ClarifyPredictionProperties


def normalize_prediction_property_arguments(
    *,
    arguments: PredictPolymerPropertiesInput,
    latest_user_message: str,
    context: AssistantSkillContext,
    api_key: str,
    base_url: str,
    model: str,
    proxy_url: str = "",
) -> PredictPolymerPropertiesInput:
    if not arguments.properties or not context.model_enabled:
        return arguments

    catalog = build_prediction_property_catalog(context)
    available_property_names = _available_property_names(catalog)
    if not available_property_names:
        return arguments

    deterministic_result = _resolve_properties_deterministically(arguments.properties, available_property_names)
    if deterministic_result is not None:
        return arguments.model_copy(update={"all_properties": False, "properties": deterministic_result})

    try:
        raw_resolution = assistant_chat.complete_prediction_property_resolution(
            latest_user_message=latest_user_message,
            requested_properties=arguments.properties,
            property_catalog=catalog,
            api_key=api_key,
            base_url=base_url,
            model=model,
            proxy_url=proxy_url,
        )
    except (assistant_chat.AssistantChatConfigError, assistant_chat.AssistantChatModelError) as exc:
        raise PropertyResolutionError(f"预测性质语义解析失败：{exc}") from exc

    resolution = parse_property_resolution(raw_resolution)
    if isinstance(resolution, ClarifyPredictionProperties):
        raise PropertyResolutionClarification(resolution.message)
    if isinstance(resolution, UnsupportedPredictionProperties):
        raise PropertyResolutionUnsupported(resolution.message)

    resolved_properties = _dedupe(resolution.properties)
    unknown = [property_name for property_name in resolved_properties if property_name not in available_property_names]
    if unknown:
        detail = "、".join(unknown)
        raise PropertyResolutionError(f"预测性质语义解析返回了当前 catalog 外的性质：{detail}")

    return arguments.model_copy(update={"all_properties": False, "properties": resolved_properties})


def build_prediction_property_catalog(context: AssistantSkillContext) -> list[dict[str, Any]]:
    result = build_predict_properties_capability_result(context)
    properties = result.get("properties")
    if not isinstance(properties, list):
        return []
    return [property_item for property_item in properties if isinstance(property_item, dict)]


def parse_property_resolution(payload: dict[str, Any]) -> PropertyResolution:
    resolution_type = str(payload.get("type") or "").strip()
    model_by_type: dict[str, type[PropertyResolution]] = {
        "resolved": ResolvedPredictionProperties,
        "unsupported": UnsupportedPredictionProperties,
        "clarify": ClarifyPredictionProperties,
    }
    resolution_model = model_by_type.get(resolution_type)
    if resolution_model is None:
        raise PropertyResolutionError(f"预测性质语义解析返回了未知类型：{resolution_type or 'empty'}")

    try:
        return resolution_model.model_validate(payload)
    except ValidationError as exc:
        raise PropertyResolutionError(f"预测性质语义解析结果无效：{_validation_detail(exc)}") from exc


def _resolve_properties_deterministically(
    requested_properties: list[str],
    available_property_names: set[str],
) -> list[str] | None:
    resolved: list[str] = []
    try:
        for requested_property in requested_properties:
            for property_name in resolve_property_names(requested_property):
                if property_name not in available_property_names:
                    return None
                resolved.append(property_name)
    except UnsupportedPredictionPropertyError:
        return None
    return _dedupe(resolved)


def _available_property_names(catalog: list[dict[str, Any]]) -> set[str]:
    return {
        str(property_item.get("name"))
        for property_item in catalog
        if property_item.get("available") and property_item.get("name")
    }


def _dedupe(properties: list[str]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for property_name in properties:
        if property_name not in seen:
            seen.add(property_name)
            resolved.append(property_name)
    return resolved


def _validation_detail(exc: ValidationError) -> str:
    first_error = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(item) for item in first_error.get("loc", []))
    message = first_error.get("msg") or "参数校验失败。"
    return f"{location}: {message}" if location else str(message)
