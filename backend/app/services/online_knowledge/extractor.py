from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from openai import OpenAI


Paper = dict[str, Any]
ExtractionResult = dict[str, Any]
SynthesisRow = dict[str, Any]


class PolymerExtractor:
    def __init__(self, api_key: str, base_url: str, model_name: str) -> None:
        self.client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))
        self.model_name = model_name
        self.results: list[ExtractionResult] = []

    def create_extraction_prompt(self, title: str, abstract: str, mode: str) -> str:
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

Return only valid JSON. No markdown."""

    def extract_from_abstract(self, title: str, abstract: str, paper_id: str, mode: str) -> ExtractionResult:
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "You are a polymer chemistry expert. Respond only with valid JSON."},
                    {"role": "user", "content": self.create_extraction_prompt(title, abstract, mode)},
                ],
                temperature=0,
                max_tokens=4000,
            )
            result_text = response.choices[0].message.content or ""
            result = json.loads(_strip_json_fence(result_text))
            if not isinstance(result, dict):
                raise ValueError("model returned a non-object JSON value")
            result["paper_id"] = paper_id
            result["title"] = title
            return result
        except Exception as exc:
            failed_result: ExtractionResult = {
                "paper_id": paper_id,
                "title": title,
                "error": str(exc),
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
