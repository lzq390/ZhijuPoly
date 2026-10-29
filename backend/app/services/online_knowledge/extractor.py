from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from app.services.ai_client import INVALID_EXTRACTION_RESULT, clean_ai_provider_error, create_openai_client
from app.services.online_knowledge.analytics import _has_value
from app.services.usage_provider import provider_call
from app.services.private_quotas import PrivateQuotaSettings


Paper = dict[str, Any]
ExtractionResult = dict[str, Any]
SynthesisRow = dict[str, Any]


class InvalidExtractionResult(ValueError):
    def __init__(self) -> None:
        super().__init__(INVALID_EXTRACTION_RESULT)


def _optional_text_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result = {field: value.get(field) for field in fields}
    if any(item is not None and not isinstance(item, str) for item in result.values()):
        raise InvalidExtractionResult()
    return result


def _has_scientific_value(value: Any) -> bool:
    if isinstance(value, str):
        return _has_value(value)
    if isinstance(value, dict):
        return any(_has_scientific_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_scientific_value(item) for item in value)
    return False


def _validate_synthesis_reaction(value: dict[str, Any]) -> dict[str, Any]:
    result = _optional_text_fields(value, ("reaction_type", "product_name", "product_abbreviation"))
    number = value.get("reaction_number")
    if number is not None and (type(number) is not int or number < 1):
        raise InvalidExtractionResult()
    result["reaction_number"] = number

    reactants = value.get("reactants")
    if reactants is not None and (
        not isinstance(reactants, list) or any(not isinstance(item, str) for item in reactants)
    ):
        raise InvalidExtractionResult()
    result["reactants"] = reactants

    properties = value.get("properties")
    if properties is not None:
        if not isinstance(properties, list) or any(not isinstance(item, dict) for item in properties):
            raise InvalidExtractionResult()
        properties = [
            _optional_text_fields(item, ("property_name", "value", "unit", "measurement_condition"))
            for item in properties
        ]
        if any(not _has_scientific_value(item.get("property_name")) and not _has_scientific_value(item.get("value"))
               for item in properties):
            raise InvalidExtractionResult()
    result["properties"] = properties

    conditions = value.get("reaction_conditions")
    if conditions is not None:
        if not isinstance(conditions, dict):
            raise InvalidExtractionResult()
        conditions = _optional_text_fields(
            conditions, ("temperature", "time", "catalyst", "solvent", "atmosphere", "pressure", "initiator", "other")
        )
    result["reaction_conditions"] = conditions
    if not _has_scientific_value(result):
        raise InvalidExtractionResult()
    return result


def _validate_extraction_result(value: Any, mode: str) -> ExtractionResult:
    if not isinstance(value, dict):
        raise InvalidExtractionResult()
    if mode == "synthesis":
        flag, count, collection = "has_polymerization", "total_reactions", "reactions"
    elif mode == "property":
        flag, count, collection = "has_polymer", "total_data_points", "data_points"
    else:
        raise InvalidExtractionResult()
    if type(value.get(flag)) is not bool or type(value.get(count)) is not int or value[count] < 0:
        raise InvalidExtractionResult()
    records = value.get(collection)
    if not isinstance(records, list) or value[count] != len(records):
        raise InvalidExtractionResult()
    if not value[flag] and records:
        raise InvalidExtractionResult()
    if mode == "synthesis" and value[flag] and not records:
        raise InvalidExtractionResult()

    validated = []
    for record in records:
        if not isinstance(record, dict):
            raise InvalidExtractionResult()
        if mode == "synthesis":
            result = _validate_synthesis_reaction(record)
        else:
            result = _optional_text_fields(record, (
                "polymer_type", "polymer_name", "condition_name", "condition_value",
                "property_name", "property_value", "relationship",
            ))
            if not _has_scientific_value(result):
                raise InvalidExtractionResult()
        validated.append(result)
    # Model metadata and control fields (including error) never enter the result.
    return {flag: value[flag], count: value[count], collection: validated}


class PolymerExtractor:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model_name: str,
        proxy_url: str = "",
    ) -> None:
        self.client = create_openai_client(
            api_key=api_key,
            base_url=base_url,
            proxy_url=proxy_url,
        )
        self.model_name = model_name
        self.results: list[ExtractionResult] = []

    def close(self) -> None:
        self.client.close()

    def create_extraction_prompt(self, title: str, abstract: str, mode: str) -> str:
        if len(title) + len(abstract) > PrivateQuotaSettings.from_environment().model_input_characters:
            raise ValueError("Paper evidence exceeds model input quota")
        if mode == "synthesis":
            return f"""You are an expert polymer chemist. Analyze this paper abstract and extract COMPLETE polymerization reaction information.

**Paper Title**: {title}

**Paper Abstract**: {abstract}

---

**INSTRUCTIONS**:

1. First, determine if there are ANY polymerization reactions in the abstract.
   - If NO polymerization reactions exist, respond with: {{"has_polymerization": false, "total_reactions": 0, "reactions": []}}
   - If polymerization reactions exist, continue to step 2.

2. Count how many DIFFERENT polymerization reactions are described.

3. For EACH reaction, extract the following information:

**CRITICAL RULES**:
- Extract ONLY information EXPLICITLY stated in the abstract.
- For reactant names, use FULL CHEMICAL NAMES, NOT abbreviations.
- If information is not mentioned, use null.
- Do NOT invent or assume any information.
- total_reactions must equal the length of reactions. Do not return empty placeholder reactions.

**IMPORTANT FOR TEMPERATURE**:
- Extract temperature even if expressed in TEXT form, such as:
  - "room temperature", "RT", "ambient temperature" -> record as "room temperature" or "RT"
  - "reflux", "refluxing" -> record as "reflux"
  - "ice bath", "0 C" -> record as "ice bath" or "0 C"
  - "heated", "heating" -> record as "heating"
  - "elevated temperature" -> record as "elevated temperature"
  - Any numeric temperature like "280 C", "100-150 C" -> record exactly as stated
- DO NOT leave temperature as null if ANY temperature indication is present.

**Required JSON format**:

{{
  "has_polymerization": true or false,
  "total_reactions": number,
  "reactions": [
    {{
      "reaction_number": integer,
      "reaction_type": "type of polymerization or null",
      "reactants": ["full chemical name 1", "full chemical name 2", ...] or null,
      "product_name": "full product name or null",
      "product_abbreviation": "abbreviation or null",
      "properties": [
        {{
          "property_name": "property name (e.g., Mn, Mw, Tg, Tm, PDI)",
          "value": "numeric value OR qualitative description",
          "unit": "unit (if numeric value) or null",
          "measurement_condition": "condition or null"
        }}
      ] or null,
      "reaction_conditions": {{
        "temperature": "MUST extract if mentioned - can be numeric (280 C) OR text (room temperature, RT, reflux, ice bath, heating, etc.) or null if truly not mentioned",
        "time": "value with unit or null",
        "catalyst": "name or null",
        "solvent": "name or null",
        "atmosphere": "name (nitrogen, argon, air, vacuum, etc.) or null",
        "pressure": "value with unit or null",
        "initiator": "name or null",
        "other": "other conditions or null"
      }}
    }}
  ]
}}

Return ONLY valid JSON. No explanations. No markdown. No code blocks."""

        return f"""You are an expert polymer chemist. Analyze this paper abstract and extract property-condition relationships.

Title: {title}
Abstract: {abstract}

Required JSON format:
{{
  "has_polymer": true,
  "total_data_points": number,
  "data_points": [
    {{
      "polymer_type": "type of polymer",
      "polymer_name": "full name",
      "condition_name": "condition name",
      "condition_value": "value with unit",
      "property_name": "property name",
      "property_value": "value with unit",
      "relationship": "direct/inverse/optimal"
    }}
  ]
}}

If no polymer is described, return has_polymer=false, total_data_points=0, and data_points=[].
If a polymer is described but no property-condition data is stated, return has_polymer=true,
total_data_points=0, and data_points=[]. Use null for individual fields not stated in the abstract.
Do not invent information or return placeholder data points. total_data_points must equal the array length.

Return only valid JSON. No markdown."""

    def extract_from_abstract(self, title: str, abstract: str, paper_id: str, mode: str) -> ExtractionResult:
        try:
            response = provider_call(self.client.chat.completions.create,
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "You are a polymer chemistry expert. Respond only with valid JSON."},
                    {"role": "user", "content": self.create_extraction_prompt(title, abstract, mode)},
                ],
                temperature=0,
                max_tokens=4000,
            )
            try:
                result_text = response.choices[0].message.content
            except (AttributeError, IndexError, TypeError):
                raise InvalidExtractionResult() from None
            if not isinstance(result_text, str):
                raise InvalidExtractionResult()
            if len(result_text.encode("utf-8")) > PrivateQuotaSettings.from_environment().model_output_bytes:
                raise ValueError("Model output exceeds retention quota")
            try:
                decoded = json.loads(_strip_json_fence(result_text))
            except (ValueError, TypeError):
                raise InvalidExtractionResult() from None
            result = _validate_extraction_result(decoded, mode)
            result["paper_id"] = paper_id
            result["title"] = title
            return result
        except Exception as exc:
            failed_result: ExtractionResult = {
                "paper_id": paper_id,
                "title": title,
                "error": clean_ai_provider_error(exc),
            }
            if mode == "property":
                failed_result.update({"has_polymer": False, "data_points": []})
            else:
                failed_result.update({"has_polymerization": False, "reactions": []})
            return failed_result

    def process_papers(
        self,
        papers: list[Paper],
        mode: str = "synthesis",
        delay: float = 0.5,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[ExtractionResult]:
        self.results = []
        papers_with_abstract = [paper for paper in papers if paper.get("abstract")]
        total_papers = len(papers_with_abstract)
        if progress_callback is not None:
            progress_callback(0, total_papers)

        for index, paper in enumerate(papers_with_abstract):
            result = self.extract_from_abstract(
                title=str(paper.get("title") or ""),
                abstract=str(paper.get("abstract") or ""),
                paper_id=str(paper.get("doi") or f"paper_{index}"),
                mode=mode,
            )
            self.results.append(result)
            if progress_callback is not None:
                progress_callback(index + 1, total_papers)
            if delay > 0 and index < len(papers_with_abstract) - 1:
                time.sleep(delay)

        return self.results

    def convert_to_rows(self, mode: str = "synthesis") -> list[SynthesisRow]:
        if mode == "property":
            return self.convert_to_property_rows()
        return self.convert_to_synthesis_rows()

    def convert_to_synthesis_rows(self) -> list[SynthesisRow]:
        rows: list[SynthesisRow] = []

        for result in self.results:
            if not result.get("has_polymerization") or not isinstance(result.get("reactions"), list):
                continue

            for reaction in result["reactions"]:
                if not isinstance(reaction, dict):
                    continue

                row: SynthesisRow = {
                    "paper_id": result.get("paper_id"),
                    "paper_title": result.get("title"),
                    "reaction_number": reaction.get("reaction_number"),
                    "reaction_type": reaction.get("reaction_type"),
                    "product_name": reaction.get("product_name"),
                    "product_abbreviation": reaction.get("product_abbreviation"),
                }

                reactants = reaction.get("reactants")
                if isinstance(reactants, list):
                    for index, reactant in enumerate(reactants, 1):
                        row[f"reactant_{index}"] = reactant

                properties = reaction.get("properties")
                if isinstance(properties, list):
                    for index, prop in enumerate(properties, 1):
                        if not isinstance(prop, dict):
                            continue
                        row[f"property_name_{index}"] = prop.get("property_name")
                        row[f"property_value_{index}"] = prop.get("value")
                        row[f"property_unit_{index}"] = prop.get("unit")
                        row[f"property_condition_{index}"] = prop.get("measurement_condition")

                conditions = reaction.get("reaction_conditions")
                if isinstance(conditions, dict):
                    row["temperature"] = conditions.get("temperature")
                    row["time"] = conditions.get("time")
                    row["catalyst"] = conditions.get("catalyst")
                    row["solvent"] = conditions.get("solvent")
                    row["atmosphere"] = conditions.get("atmosphere")
                    row["pressure"] = conditions.get("pressure")
                    row["initiator"] = conditions.get("initiator")
                    row["other_conditions"] = conditions.get("other")

                rows.append(row)

        return rows

    def convert_to_property_rows(self) -> list[SynthesisRow]:
        rows: list[SynthesisRow] = []

        for result in self.results:
            if not result.get("has_polymer") or not isinstance(result.get("data_points"), list):
                continue

            for index, data_point in enumerate(result["data_points"], 1):
                if not isinstance(data_point, dict):
                    continue

                rows.append(
                    {
                        "paper_id": result.get("paper_id"),
                        "paper_title": result.get("title"),
                        "data_point_number": index,
                        "polymer_type": data_point.get("polymer_type"),
                        "polymer_name": data_point.get("polymer_name"),
                        "condition_name": data_point.get("condition_name"),
                        "condition_value": data_point.get("condition_value"),
                        "property_name": data_point.get("property_name"),
                        "property_value": data_point.get("property_value"),
                        "relationship": data_point.get("relationship"),
                    }
                )

        return rows


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()
