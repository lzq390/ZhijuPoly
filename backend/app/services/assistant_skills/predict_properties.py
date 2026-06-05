from __future__ import annotations

import re
from time import perf_counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.assistant_skills.registry import (
    AssistantSkill,
    AssistantSkillContext,
    SkillClarificationRequired,
    SkillExecutionError,
)
from app.services.predictor import (
    PROPERTY_LABELS_ZH,
    PROPERTY_MODELS,
    PROPERTY_UNITS,
    get_available_properties,
    predict,
)
from app.utils.exceptions import InvalidSmilesError, ModelArtifactError, UnsupportedPredictionPropertyError

PREDICT_POLYMER_PROPERTIES_SKILL = "predict_polymer_properties"
ALL_PREDICTABLE_PROPERTIES = tuple(PROPERTY_MODELS.keys())

THERMAL_PROPERTY_GROUP = (
    "Glass transition temperature",
    "Melting temperature",
    "Thermal decomposition temperature",
    "Thermal decomposition weight loss",
)
MECHANICAL_PROPERTY_GROUP = (
    "Elongation at break",
    "Tensile stress strength at break",
)
PERMEABILITY_PROPERTY_GROUP = (
    "O2 Permeability Barrer",
    "Co2 Permeability Barrer",
    "H2 Permeability Barrer",
)

PROPERTY_GROUP_ALIASES: dict[str, tuple[str, ...]] = {
    "热学性质": THERMAL_PROPERTY_GROUP,
    "热学性能": THERMAL_PROPERTY_GROUP,
    "热性质": THERMAL_PROPERTY_GROUP,
    "热性能": THERMAL_PROPERTY_GROUP,
    "热相关性质": THERMAL_PROPERTY_GROUP,
    "thermal": THERMAL_PROPERTY_GROUP,
    "thermal property": THERMAL_PROPERTY_GROUP,
    "thermal properties": THERMAL_PROPERTY_GROUP,
    "thermal performance": THERMAL_PROPERTY_GROUP,
    "力学性质": MECHANICAL_PROPERTY_GROUP,
    "力学性能": MECHANICAL_PROPERTY_GROUP,
    "机械性质": MECHANICAL_PROPERTY_GROUP,
    "机械性能": MECHANICAL_PROPERTY_GROUP,
    "mechanical": MECHANICAL_PROPERTY_GROUP,
    "mechanical property": MECHANICAL_PROPERTY_GROUP,
    "mechanical properties": MECHANICAL_PROPERTY_GROUP,
    "渗透性": PERMEABILITY_PROPERTY_GROUP,
    "气体渗透性": PERMEABILITY_PROPERTY_GROUP,
    "气体透过性": PERMEABILITY_PROPERTY_GROUP,
    "permeability": PERMEABILITY_PROPERTY_GROUP,
    "gas permeability": PERMEABILITY_PROPERTY_GROUP,
}

PROPERTY_ALIASES: dict[str, str] = {
    "tg": "Glass transition temperature",
    "玻璃化转变温度": "Glass transition temperature",
    "玻璃化温度": "Glass transition temperature",
    "glass transition": "Glass transition temperature",
    "glass transition temperature": "Glass transition temperature",
    "tm": "Melting temperature",
    "熔融温度": "Melting temperature",
    "熔点": "Melting temperature",
    "melting temperature": "Melting temperature",
    "td": "Thermal decomposition temperature",
    "td5": "Thermal decomposition temperature",
    "热分解温度": "Thermal decomposition temperature",
    "thermal decomposition temperature": "Thermal decomposition temperature",
    "热分解失重率": "Thermal decomposition weight loss",
    "热失重": "Thermal decomposition weight loss",
    "thermal decomposition weight loss": "Thermal decomposition weight loss",
    "weight loss": "Thermal decomposition weight loss",
    "断裂伸长率": "Elongation at break",
    "elongation": "Elongation at break",
    "elongation at break": "Elongation at break",
    "断裂拉伸强度": "Tensile stress strength at break",
    "拉伸强度": "Tensile stress strength at break",
    "tensile strength": "Tensile stress strength at break",
    "tensile stress strength at break": "Tensile stress strength at break",
    "o2": "O2 Permeability Barrer",
    "o2 permeability": "O2 Permeability Barrer",
    "氧气渗透性": "O2 Permeability Barrer",
    "co2": "Co2 Permeability Barrer",
    "co2 permeability": "Co2 Permeability Barrer",
    "二氧化碳渗透性": "Co2 Permeability Barrer",
    "h2": "H2 Permeability Barrer",
    "h2 permeability": "H2 Permeability Barrer",
    "氢气渗透性": "H2 Permeability Barrer",
}


class PredictPolymerPropertiesInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    smiles: str = Field(min_length=1)
    properties: list[str] = Field(default_factory=list)
    all_properties: bool = True

    @field_validator("properties")
    @classmethod
    def normalize_properties(cls, properties: list[str]) -> list[str]:
        normalized = [value.strip() for value in properties]
        if any(not value for value in normalized):
            raise ValueError("prediction property names must not be empty")
        return normalized


def build_predict_properties_skill() -> AssistantSkill:
    return AssistantSkill(
        name=PREDICT_POLYMER_PROPERTIES_SKILL,
        display_name="聚合物性质预测",
        description="根据 SMILES 预测聚合物 9 个性质或指定性质。",
        input_model=PredictPolymerPropertiesInput,
        executor=execute_predict_polymer_properties,
    )


def execute_predict_polymer_properties(
    arguments: BaseModel,
    context: AssistantSkillContext,
) -> dict[str, Any]:
    if not isinstance(arguments, PredictPolymerPropertiesInput):
        raise SkillExecutionError("invalid prediction skill arguments")
    if not context.model_enabled:
        raise SkillExecutionError("预测服务未启用，请确认 MODEL_ENABLED=true。")

    smiles = arguments.smiles.strip()

    started_at = perf_counter()
    try:
        property_names = _resolve_requested_properties(arguments)
        predictions = predict(smiles, property_names, model_dir=context.model_dir)
    except (InvalidSmilesError, UnsupportedPredictionPropertyError, ModelArtifactError) as exc:
        raise SkillExecutionError(str(exc)) from exc

    return {
        "type": PREDICT_POLYMER_PROPERTIES_SKILL,
        "smiles": smiles,
        "predictions": predictions,
        "properties": [
            {
                "name": property_name,
                "label_zh": PROPERTY_LABELS_ZH[property_name],
                "unit": PROPERTY_UNITS[property_name],
                "value": predictions[property_name],
            }
            for property_name in property_names
        ],
        "query_time_ms": (perf_counter() - started_at) * 1000,
    }


def build_predict_properties_capability_result(context: AssistantSkillContext) -> dict[str, Any]:
    available_properties = set(get_available_properties(context.model_dir)) if context.model_enabled else set()
    return {
        "type": f"{PREDICT_POLYMER_PROPERTIES_SKILL}_capability",
        "skill_name": PREDICT_POLYMER_PROPERTIES_SKILL,
        "display_name": "聚合物性质预测",
        "model_enabled": context.model_enabled,
        "supported_count": len(available_properties),
        "registered_count": len(ALL_PREDICTABLE_PROPERTIES),
        "properties": [
            {
                "name": property_name,
                "label_zh": PROPERTY_LABELS_ZH[property_name],
                "unit": PROPERTY_UNITS[property_name],
                "available": property_name in available_properties,
            }
            for property_name in ALL_PREDICTABLE_PROPERTIES
        ],
    }


def format_predict_properties_capability(result: dict[str, Any]) -> str:
    properties = result.get("properties") or []
    if not isinstance(properties, list):
        properties = []

    available = [
        property_item
        for property_item in properties
        if isinstance(property_item, dict) and property_item.get("available")
    ]
    unavailable = [
        property_item
        for property_item in properties
        if isinstance(property_item, dict) and not property_item.get("available")
    ]
    model_enabled = bool(result.get("model_enabled"))

    if model_enabled:
        lines = [f"当前预测接口可预测 {len(available)} 个性质："]
        listed_properties = available
    else:
        lines = ["预测服务当前未启用（MODEL_ENABLED=false）。已注册的预测性质有："]
        listed_properties = [property_item for property_item in properties if isinstance(property_item, dict)]

    for index, property_item in enumerate(listed_properties, start=1):
        lines.append(
            f"{index}. {property_item['label_zh']}（{property_item['name']}），单位：{property_item['unit']}"
        )

    if model_enabled and unavailable:
        missing = "、".join(str(property_item.get("label_zh")) for property_item in unavailable)
        lines.append(f"另有 {len(unavailable)} 个注册性质缺少模型文件，当前不可预测：{missing}。")

    return "\n".join(lines)


def _resolve_requested_properties(arguments: PredictPolymerPropertiesInput) -> list[str]:
    if not arguments.properties:
        return list(ALL_PREDICTABLE_PROPERTIES)

    resolved: list[str] = []
    seen: set[str] = set()
    for value in arguments.properties:
        for property_name in resolve_property_names(value):
            if property_name not in seen:
                seen.add(property_name)
                resolved.append(property_name)
    return resolved


def resolve_property_names(value: str) -> list[str]:
    normalized = _property_key(value)
    property_group = PROPERTY_GROUP_ALIASES.get(normalized)
    if property_group:
        return list(property_group)
    return [resolve_property_name(value)]


def resolve_property_name(value: str) -> str:
    stripped = value.strip()
    if stripped in PROPERTY_MODELS:
        return stripped

    normalized = _property_key(stripped)
    for property_name in PROPERTY_MODELS:
        if normalized == _property_key(property_name):
            return property_name
        if normalized == _property_key(PROPERTY_LABELS_ZH[property_name]):
            return property_name

    alias = PROPERTY_ALIASES.get(normalized)
    if alias:
        return alias

    raise UnsupportedPredictionPropertyError(f"unsupported prediction property: {value}")


def _property_key(value: str) -> str:
    normalized = value.lower().replace("₂", "2").replace("二氧化碳", "co2")
    normalized = normalized.replace("氧气", "o2").replace("氢气", "h2")
    return re.sub(r"[\s_\-]+", " ", normalized).strip()
