from __future__ import annotations

import re
from collections import Counter
from typing import Any


SynthesisRow = dict[str, Any]


def standardize_temperature(temp_str: Any) -> tuple[int | None, str | None]:
    if temp_str is None:
        return None, None

    text = str(temp_str).lower().strip()
    if not text:
        return None, None

    temp_mappings = {
        "room temperature": (25, "Room Temperature (RT, ~25°C)"),
        "rt": (25, "Room Temperature (RT, ~25°C)"),
        "ambient": (25, "Room Temperature (RT, ~25°C)"),
        "ambient temperature": (25, "Room Temperature (RT, ~25°C)"),
        "25°c": (25, "Room Temperature (RT, ~25°C)"),
        "25 °c": (25, "Room Temperature (RT, ~25°C)"),
        "25℃": (25, "Room Temperature (RT, ~25°C)"),
        "ice bath": (0, "Ice Bath (~0°C)"),
        "ice-bath": (0, "Ice Bath (~0°C)"),
        "0°c": (0, "Ice Bath (~0°C)"),
        "0 °c": (0, "Ice Bath (~0°C)"),
        "reflux": (100, "Reflux"),
        "refluxing": (100, "Reflux"),
        "heating": (80, "Heating"),
        "heated": (80, "Heating"),
        "elevated temperature": (60, "Elevated Temperature"),
        "elevated": (60, "Elevated Temperature"),
        "low temperature": (4, "Low Temperature"),
        "cold": (4, "Low Temperature"),
        "cooling": (4, "Low Temperature"),
        "body temperature": (37, "Body Temperature (37°C)"),
        "physiological": (37, "Body Temperature (37°C)"),
        "37°c": (37, "Body Temperature (37°C)"),
        "37 °c": (37, "Body Temperature (37°C)"),
    }

    for key, (value, label) in temp_mappings.items():
        if key in text:
            return value, label

    for pattern in [
        r"(\d+)\s*[-–]\s*(\d+)\s*[°℃]?\s*c",
        r"(\d+)\s*(?:to)\s*(\d+)\s*[°℃]?\s*c",
        r"(\d+)\s*[°℃]\s*c",
        r"(\d+)\s*°\s*c",
        r"(\d+)\s*℃",
        r"(\d+)\s*c\b",
        r"(\d+)\s*k\b",
    ]:
        match = re.search(pattern, text)
        if not match:
            continue
        groups = match.groups()
        if len(groups) == 2 and groups[1] is not None:
            val1, val2 = int(groups[0]), int(groups[1])
            return (val1 + val2) // 2, f"{val1}-{val2}°C"
        value = int(groups[0])
        if "k" in text and value > 200:
            value -= 273
        return value, f"{value}°C"

    return None, text


def categorize_temperature(temp_value: int | None) -> str:
    if temp_value is None:
        return "Unknown"
    if temp_value <= 0:
        return "≤0°C"
    if temp_value <= 30:
        return "0-30°C"
    if temp_value <= 60:
        return "30-60°C"
    if temp_value <= 100:
        return "60-100°C"
    if temp_value <= 150:
        return "100-150°C"
    if temp_value <= 200:
        return "150-200°C"
    if temp_value <= 300:
        return "200-300°C"
    return ">300°C"


def build_stats(
    papers: list[dict[str, Any]],
    rows: list[SynthesisRow],
    example_used: bool,
    mode: str = "synthesis",
) -> dict[str, Any]:
    if mode == "property":
        return {
            "totalPapers": len(papers),
            "totalDataPoints": len(rows),
            "withProperties": len(rows),
            "avgReliability": 78,
            "exampleUsed": example_used,
            "hasPolymerName": _count_present(rows, "polymer_name"),
            "hasCondition": _count_present(rows, "condition_name"),
            "hasConditionValue": _count_present(rows, "condition_value"),
            "hasPropertyName": _count_present(rows, "property_name"),
            "hasPropertyValue": _count_present(rows, "property_value"),
            "hasRelationship": _count_present(rows, "relationship"),
        }

    return {
        "totalPapers": len(papers),
        "withSynthesis": len(rows),
        "avgReliability": 78,
        "exampleUsed": example_used,
        "hasReactants": _count_present(rows, "reactant_1"),
        "hasTemperature": _count_present(rows, "temperature"),
        "hasCatalyst": _count_present(rows, "catalyst"),
        "hasTime": _count_present(rows, "time"),
        "hasAtmosphere": _count_present(rows, "atmosphere"),
        "hasPressure": _count_present(rows, "pressure"),
        "hasInitiator": _count_present(rows, "initiator"),
    }


def build_syntheses(rows: list[SynthesisRow], limit: int = 15) -> list[dict[str, str]]:
    syntheses: list[dict[str, str]] = []

    for row in rows[:limit]:
        reactants = [
            str(row[f"reactant_{index}"])
            for index in range(1, 10)
            if _has_value(row.get(f"reactant_{index}"))
        ]
        properties = []
        for index in range(1, 5):
            prop_name = row.get(f"property_name_{index}")
            if not _has_value(prop_name):
                continue
            prop_value = row.get(f"property_value_{index}")
            prop_unit = row.get(f"property_unit_{index}")
            value = f"{prop_name}: {prop_value or ''} {prop_unit or ''}".strip()
            properties.append(value)

        syntheses.append(
            {
                "method": _display(row.get("reaction_type"), "Polymerization"),
                "reaction_type": _display(row.get("reaction_type")),
                "product_name": _display(row.get("product_name")),
                "product_abbreviation": _display(row.get("product_abbreviation")),
                "temperature": _display(row.get("temperature")),
                "catalyst": _display(row.get("catalyst")),
                "solvent": _display(row.get("solvent")),
                "time": _display(row.get("time")),
                "atmosphere": _display(row.get("atmosphere")),
                "pressure": _display(row.get("pressure")),
                "initiator": _display(row.get("initiator")),
                "reactants": ", ".join(reactants) if reactants else "Not Provided",
                "properties": "; ".join(properties) if properties else "Not Provided",
            }
        )

    return syntheses


def build_property_points(rows: list[SynthesisRow], limit: int = 30) -> list[dict[str, str]]:
    property_points: list[dict[str, str]] = []

    for row in rows[:limit]:
        property_points.append(
            {
                "polymer_type": _display(row.get("polymer_type")),
                "polymer_name": _display(row.get("polymer_name")),
                "condition_name": _display(row.get("condition_name")),
                "condition_value": _display(row.get("condition_value")),
                "property_name": _display(row.get("property_name")),
                "property_value": _display(row.get("property_value")),
                "relationship": _display(row.get("relationship")),
                "paper_title": _display(row.get("paper_title")),
            }
        )

    return property_points


def temperature_distribution(rows: list[SynthesisRow]) -> list[dict[str, Any]]:
    categories = []
    for row in rows:
        value, _ = standardize_temperature(row.get("temperature"))
        category = categorize_temperature(value)
        if category != "Unknown":
            categories.append(category)
    return _counter_table(Counter(categories), limit=9)


def solvent_distribution(rows: list[SynthesisRow]) -> list[dict[str, Any]]:
    return _counter_table(_value_counter(rows, "solvent"), limit=6)


def catalyst_table(rows: list[SynthesisRow]) -> list[dict[str, Any]]:
    return _counter_table(_value_counter(rows, "catalyst"), limit=8)


def temperature_labels(rows: list[SynthesisRow]) -> list[dict[str, Any]]:
    labels = []
    for row in rows:
        _, label = standardize_temperature(row.get("temperature"))
        if label:
            labels.append(label)
    return _counter_table(Counter(labels), limit=10)


def reaction_type_table(rows: list[SynthesisRow]) -> list[dict[str, Any]]:
    return _counter_table(_value_counter(rows, "reaction_type"), limit=8)


def property_name_distribution(rows: list[SynthesisRow]) -> list[dict[str, Any]]:
    return _counter_table(_value_counter(rows, "property_name"), limit=8)


def property_condition_distribution(rows: list[SynthesisRow]) -> list[dict[str, Any]]:
    return _counter_table(_value_counter(rows, "condition_name"), limit=8)


def polymer_type_distribution(rows: list[SynthesisRow]) -> list[dict[str, Any]]:
    return _counter_table(_value_counter(rows, "polymer_type"), limit=8)


def relationship_distribution(rows: list[SynthesisRow]) -> list[dict[str, Any]]:
    return _counter_table(_value_counter(rows, "relationship"), limit=8)


def condition_summary(rows: list[SynthesisRow], mode: str = "synthesis") -> list[str]:
    if mode == "property":
        return property_condition_summary(rows)

    summary: list[str] = []
    for field, label in [
        ("time", "Reaction time"),
        ("atmosphere", "Atmosphere"),
        ("pressure", "Pressure"),
        ("initiator", "Initiator"),
    ]:
        values = [str(row.get(field)) for row in rows if _has_value(row.get(field))]
        if values:
            most_common = Counter(values).most_common(1)[0][0]
            summary.append(f"{label}: {len(values)} records; most common: {most_common}")

    return summary or ["Limited condition data available"]


def property_condition_summary(rows: list[SynthesisRow]) -> list[str]:
    summary: list[str] = []
    conditions = [str(row.get("condition_name")) for row in rows if _has_value(row.get("condition_name"))]
    condition_values = [str(row.get("condition_value")) for row in rows if _has_value(row.get("condition_value"))]
    relationships = [str(row.get("relationship")) for row in rows if _has_value(row.get("relationship"))]

    if conditions:
        summary.append(f"Conditions: {len(conditions)} records; most common: {Counter(conditions).most_common(1)[0][0]}")
    if condition_values:
        summary.append(f"Condition values: {len(condition_values)} records; most common: {Counter(condition_values).most_common(1)[0][0]}")
    if relationships:
        summary.append(f"Relationships: {len(relationships)} records; most common: {Counter(relationships).most_common(1)[0][0]}")

    return summary or ["Limited property-condition data available"]


def create_example_data(material: str, mode: str = "synthesis") -> list[dict[str, str]]:
    material_lower = material.lower()
    examples = {
        "polyethylene": {
            "title": "Synthesis of Polyethylene",
            "abstract": "Polyethylene was synthesized via coordination polymerization using Ziegler-Natta catalyst at 80°C and 5 bar pressure. The polymerization was carried out in hexane solvent for 2 hours. The resulting polymer had Mw = 150,000 g/mol and melting point 135°C.",
        },
        "polyimide": {
            "title": "Synthesis of Aromatic Polyimide",
            "abstract": "Aromatic polyimide was synthesized from pyromellitic dianhydride and 4,4'-oxydianiline in DMAc solvent. The poly(amic acid) intermediate was thermally imidized at 300°C for 2 hours. The resulting polyimide showed Tg > 400°C and excellent thermal stability.",
        },
        "nylon": {
            "title": "Nylon 6 Synthesis",
            "abstract": "Nylon 6 was prepared by ring-opening polymerization of epsilon-caprolactam at 250°C using 6-aminocaproic acid as initiator. The reaction was carried out under nitrogen atmosphere for 6 hours.",
        },
        "pla": {
            "title": "PLA Synthesis",
            "abstract": "Poly(L-lactic acid) was synthesized via ring-opening polymerization of L-lactide using tin(II) 2-ethylhexanoate as catalyst and benzyl alcohol as initiator. Polymerization was conducted in toluene at 130°C for 24 hours under argon.",
        },
        "pet": {
            "title": "PET Synthesis",
            "abstract": "Poly(ethylene terephthalate) was synthesized by melt polycondensation of terephthalic acid and ethylene glycol using antimony trioxide as catalyst. The reaction was carried out at 280°C for 3 hours under nitrogen.",
        },
    }

    for key, example in examples.items():
        if key in material_lower:
            return [{"title": example["title"], "abstract": example["abstract"], "doi": f"example_{key}"}]

    return [
        {
            "title": f"Synthesis of {material}",
            "abstract": f"{material} was synthesized via polymerization reaction. The reaction was carried out at 200°C using appropriate catalyst in solvent. The polymer showed good thermal stability and mechanical properties.",
            "doi": "example_general",
        }
    ]


def _counter_table(counter: Counter[str], limit: int) -> list[dict[str, Any]]:
    total = sum(counter.values())
    if total == 0:
        return []
    return [
        {"label": label, "count": int(count), "percentage": round(count / total * 100, 1)}
        for label, count in counter.most_common(limit)
    ]


def _value_counter(rows: list[SynthesisRow], field: str) -> Counter[str]:
    return Counter(str(row.get(field)) for row in rows if _has_value(row.get(field)))


def _count_present(rows: list[SynthesisRow], field: str) -> int:
    return sum(1 for row in rows if _has_value(row.get(field)))


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return bool(text) and text.lower() not in {"nan", "none", "null", "n/a", "not provided"}


def _display(value: Any, fallback: str = "Not Provided") -> str:
    return str(value) if _has_value(value) else fallback
