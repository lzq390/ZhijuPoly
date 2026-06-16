from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlparse

from openai import OpenAI

from app.models import AssistantChatMessage, AssistantModuleContext


class AssistantChatConfigError(RuntimeError):
    pass


class AssistantChatModelError(RuntimeError):
    pass


def parse_assistant_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AssistantChatModelError("assistant intent response was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise AssistantChatModelError("assistant intent response must be a JSON object")
    return parsed


def validate_assistant_model_access(*, api_key: str, base_url: str, model: str) -> None:
    if not api_key:
        raise AssistantChatConfigError("Assistant API Key is required")
    if not model:
        raise AssistantChatConfigError("Assistant model is required")
    parsed = urlparse(base_url)
    if not parsed.scheme or not parsed.hostname:
        raise AssistantChatConfigError("Assistant Base URL must include protocol and host")


def complete_assistant_intent(
    *,
    messages: Sequence[AssistantChatMessage],
    modules: Sequence[AssistantModuleContext],
    active_module: str | None,
    skill_catalog: str,
    api_key: str,
    base_url: str,
    model: str,
) -> dict[str, Any]:
    validate_assistant_model_access(api_key=api_key, base_url=base_url, model=model)
    client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": build_assistant_intent_prompt(
                        modules=modules,
                        active_module=active_module,
                        skill_catalog=skill_catalog,
                    ),
                },
                *[{"role": message.role, "content": message.content} for message in messages],
            ],
            temperature=0,
            max_tokens=700,
        )
        if not response.choices:
            raise AssistantChatModelError("assistant intent response had no choices")
        content = getattr(response.choices[0].message, "content", None)
        if not content:
            raise AssistantChatModelError("assistant intent response was empty")
        return parse_assistant_json(content)
    except AssistantChatConfigError:
        raise
    except AssistantChatModelError:
        raise
    except Exception as exc:
        raise AssistantChatModelError(str(exc)) from exc


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


def stream_assistant_image_chat(
    *,
    messages: Sequence[AssistantChatMessage],
    modules: Sequence[AssistantModuleContext],
    active_module: str | None,
    image_bytes: bytes,
    content_type: str,
    api_key: str,
    base_url: str,
    model: str,
) -> Iterable[str]:
    validate_assistant_model_access(api_key=api_key, base_url=base_url, model=model)
    if not messages or messages[-1].role != "user":
        raise AssistantChatModelError("latest assistant image chat message must be from the user")

    client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))
    image_data_url = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    prior_messages = [{"role": message.role, "content": message.content} for message in messages[:-1]]
    latest_message = messages[-1]

    try:
        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": build_assistant_image_system_prompt(modules, active_module)},
                *prior_messages,
                {
                    "role": latest_message.role,
                    "content": [
                        {"type": "text", "text": latest_message.content},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_data_url,
                            },
                        },
                    ],
                },
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


def stream_assistant_skill_summary(
    *,
    messages: Sequence[AssistantChatMessage],
    modules: Sequence[AssistantModuleContext],
    active_module: str | None,
    skill_name: str,
    skill_result: dict[str, Any],
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
                {
                    "role": "system",
                    "content": build_assistant_skill_summary_prompt(
                        modules=modules,
                        active_module=active_module,
                        skill_name=skill_name,
                    ),
                },
                *[{"role": message.role, "content": message.content} for message in messages],
                {
                    "role": "assistant",
                    "content": "Skill execution result JSON:\n" + json.dumps(skill_result, ensure_ascii=False),
                },
            ],
            temperature=0.2,
            max_tokens=900,
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


def complete_prediction_property_resolution(
    *,
    latest_user_message: str,
    requested_properties: Sequence[str],
    property_catalog: Sequence[dict[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
) -> dict[str, Any]:
    validate_assistant_model_access(api_key=api_key, base_url=base_url, model=model)
    client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": build_prediction_property_resolution_prompt(
                        requested_properties=requested_properties,
                        property_catalog=property_catalog,
                    ),
                },
                {"role": "user", "content": latest_user_message},
            ],
            temperature=0,
            max_tokens=700,
        )
        if not response.choices:
            raise AssistantChatModelError("prediction property resolver response had no choices")
        content = getattr(response.choices[0].message, "content", None)
        if not content:
            raise AssistantChatModelError("prediction property resolver response was empty")
        return parse_assistant_json(content)
    except AssistantChatConfigError:
        raise
    except AssistantChatModelError:
        raise
    except Exception as exc:
        raise AssistantChatModelError(str(exc)) from exc


def build_prediction_property_resolution_prompt(
    *,
    requested_properties: Sequence[str],
    property_catalog: Sequence[dict[str, Any]],
) -> str:
    return f"""You are the ZhijuPoly prediction property resolver.

Return JSON only. Do not include Markdown.

Your task is semantic normalization only. Map the user's requested prediction property phrases to canonical property names from the provided catalog.

Current prediction catalog JSON:
{json.dumps(list(property_catalog), ensure_ascii=False)}

Requested property phrases JSON:
{json.dumps(list(requested_properties), ensure_ascii=False)}

Rules:
- Only choose canonical property names exactly as they appear in catalog items with available=true.
- Use your polymer-domain knowledge to map broad or natural phrases, such as thermal stability, gas barrier, oxygen barrier, tensile behavior, or thermal properties, to matching catalog properties.
- If a phrase maps to a supported group, return all supported catalog properties in that group.
- If the user asks for a property that is not represented in the catalog, return unsupported. Do not choose an approximate substitute unless the user explicitly asks for the closest available proxy.
- If the request is too ambiguous to map safely, return clarify.
- Never invent property names, units, model names, endpoint paths, or prediction values.

Allowed response shapes:
{{"type":"resolved","properties":["Glass transition temperature"]}}
{{"type":"unsupported","requested":["杨氏模量"],"message":"当前预测接口暂不支持杨氏模量。"}}
{{"type":"clarify","message":"请明确要预测哪类性质。"}}
""".strip()


def build_assistant_intent_prompt(
    *,
    modules: Sequence[AssistantModuleContext],
    active_module: str | None,
    skill_catalog: str,
) -> str:
    module_lines = "\n".join(
        (
            f"- id: {module.id}; display: {module.title}; group: {module.group}; "
            f"description: {module.description}"
        )
        for module in modules
    )
    return f"""You are the ZhijuPoly assistant intent router.

Return JSON only. Do not include Markdown.

Available executable skills:
{skill_catalog}

Available modules for navigation context:
{module_lines or "- No module context was provided."}

Current active module: {active_module or "home"}

Your task:
- If the latest user message includes a [Resolved structure input] block, treat its Resolved SMILES as the user's structure input for downstream tasks.
- If the latest user message asks which properties the prediction skill/API/model supports, return skill_info for predict_polymer_properties.
- If the latest user message asks to predict, estimate, calculate, or evaluate polymer properties from a SMILES string or resolved structure input, return a skill_call for predict_polymer_properties.
- If the latest user message asks for property prediction but no SMILES or resolved structure input is present, return clarify with a short Chinese message asking for SMILES.
- If the user asks for all properties, nine properties, or 9 properties, set all_properties=true.
- If the user asks for a property group, such as thermal/热学性质, mechanical/力学性质, or permeability/渗透性, set all_properties=false and put that group wording in properties.
- If the user asks for one or more specific properties, set all_properties=false and put the requested property names in properties. Use the user's wording if needed.
- For normal polymer research questions, planning, or module guidance, return chat.

Allowed response shapes:
{{"type":"skill_call","skill_name":"predict_polymer_properties","arguments":{{"smiles":"CCO","all_properties":true}}}}
{{"type":"skill_info","skill_name":"predict_polymer_properties"}}
{{"type":"clarify","message":"请提供需要预测的 SMILES 或 IUPAC 名称。"}}
{{"type":"chat","message":"普通科研问答或模块建议。"}}
""".strip()


def build_assistant_skill_summary_prompt(
    *,
    modules: Sequence[AssistantModuleContext],
    active_module: str | None,
    skill_name: str,
) -> str:
    base_prompt = build_assistant_system_prompt(modules, active_module)
    return f"""{base_prompt}

The backend has already executed the registered skill `{skill_name}`.
Summarize only the provided skill result JSON.
Do not invent values, units, model confidence, routes, or extra endpoints.
Use the user's language. Keep the summary concise because a structured result card is shown in the UI.
""".strip()


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
- Do not claim that you executed a module, query, prediction, search, or design job unless the backend explicitly provides a skill result in this turn.
- If the user asks for automation that is not backed by a registered skill result, say that this v1 assistant can advise and navigate for that request.

Current active module: {active_module_text}

Available modules:
{module_lines or "- No module context was provided."}
""".strip()


def build_assistant_image_system_prompt(
    modules: Sequence[AssistantModuleContext],
    active_module: str | None,
) -> str:
    return f"""{build_assistant_system_prompt(modules, active_module)}

Image analysis rules:
- The latest user message includes one uploaded image. Analyze only visible image evidence and the user's text.
- Focus on polymer/materials research context when relevant: morphology, plots, spectra, instruments, documents, screenshots, or experimental observations.
- Distinguish observations from hypotheses. Do not present visual guesses as confirmed experimental conclusions.
- Do not invent SMILES, property values, units, database results, or executed tool outputs from the image.
- If the user wants to extract SMILES from a molecular structure image, predict properties from a structure, or search by structure, tell them to use the structure recognition mode.
""".strip()
