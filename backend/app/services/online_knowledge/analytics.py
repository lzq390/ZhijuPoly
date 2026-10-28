from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


SynthesisRow = dict[str, Any]


_TEMPERATURE_NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
_TEMPERATURE_UNIT = r"(?:°\s*c\b|℃|c\b|k\b)"
_TEMPERATURE_RANGE = re.compile(
    rf"(?<![\w.+-])(?P<low>{_TEMPERATURE_NUMBER})\s*"
    rf"(?P<low_unit>{_TEMPERATURE_UNIT})?\s*(?:[-–—]|\bto\b)\s*"
    rf"(?P<high>{_TEMPERATURE_NUMBER})\s*(?P<high_unit>{_TEMPERATURE_UNIT})",
    re.IGNORECASE,
)
_TEMPERATURE_SINGLE = re.compile(
    rf"(?<![\w.+-])(?P<value>{_TEMPERATURE_NUMBER})\s*(?P<unit>{_TEMPERATURE_UNIT})",
    re.IGNORECASE,
)
_TEMPERATURE_BOUND = re.compile(
    r"(?:[<>]\s*=?|[≤≥≈~]|\b(?:above|below|under|over|up to|at least|at most|"
    r"less than|more than|about|approximately))\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _TemperatureEvidence:
    lower: Decimal | None
    upper: Decimal | None
    label: str | None


def _celsius(number: str, unit: str) -> Decimal:
    value = Decimal(number)
    if unit.casefold() == "k":
        value -= Decimal("273.15")
    if not value.is_finite() or value < Decimal("-273.15"):
        raise ValueError("Invalid absolute temperature")
    return value


def _temperature_evidence(temp_str: Any) -> _TemperatureEvidence:
    raw = str(temp_str).strip() if temp_str is not None else ""
    unknown = _TemperatureEvidence(None, None, raw or None)
    if not raw:
        return unknown
    text = raw.replace("−", "-")
    ranges = list(_TEMPERATURE_RANGE.finditer(text))
    singles = [match for match in _TEMPERATURE_SINGLE.finditer(text)
               if not any(part.start() <= match.start() < part.end() for part in ranges)]
    # Never reduce multiple stages, inequalities or uncertainties to one observation.
    if len(ranges) + len(singles) != 1 or re.search(r"±|\+\s*/\s*-|°\s*f\b|℉", text, re.I):
        return unknown
    match = (ranges or singles)[0]
    if _TEMPERATURE_BOUND.search(text[:match.start()]):
        return unknown
    if re.search(r"\b(?:then|followed by|subsequently)\b", text, re.I):
        return unknown
    if re.search(r"(?:\bto\b|[-–—])\s*$", text[:match.start()], re.I):
        return unknown
    if re.match(r"\s*(?:\bto\b|[-–—]|/|\bper\b)", text[match.end():], re.I):
        return unknown
    if re.match(r"\s*(?:[<>≤≥]|(?:and|or)\s+(?:above|below|higher|lower)\b)", text[match.end():], re.I):
        return unknown
    # A unitless neighbouring value (e.g. "80 and 100 C") is ambiguous too.
    if re.search(r"\d\s*(?:and|or|/|,|[-–—])\s*$", text[:match.start()], re.I):
        return unknown
    if re.match(r"\s*(?:and|or|,)\s*[+-]?(?:\d|\.\d)", text[match.end():], re.I):
        return unknown
    try:
        if ranges:
            high_unit = match["high_unit"]
            low = _celsius(match["low"], match["low_unit"] or high_unit)
            high = _celsius(match["high"], high_unit)
            if low > high:
                return unknown
            return _TemperatureEvidence(low, high, raw)
        value = _celsius(match["value"], match["unit"])
        return _TemperatureEvidence(value, value, f"{value.normalize():f}°C")
    except (InvalidOperation, ValueError):
        return unknown


def standardize_temperature(temp_str: Any) -> tuple[float | None, str | None]:
    evidence = _temperature_evidence(temp_str)
    # Ranges keep their source label; no midpoint is presented as a measured value.
    value = evidence.lower if evidence.lower == evidence.upper else None
    return (float(value) if value is not None else None), evidence.label


def categorize_temperature(temp_value: float | Decimal | None) -> str:
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
        evidence = _temperature_evidence(row.get("temperature"))
        category = categorize_temperature(evidence.lower)
        if category != "Unknown" and category == categorize_temperature(evidence.upper):
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
