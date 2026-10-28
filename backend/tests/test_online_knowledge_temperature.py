"""Temperature evidence must not invent or silently truncate scientific values."""
from copy import deepcopy

import pytest

from app.models import OnlineKnowledgeSearchResponse
from app.services.online_knowledge.analytics import (
    build_syntheses,
    standardize_temperature,
    temperature_distribution,
    temperature_labels,
)


@pytest.mark.parametrize("raw,expected", [
    ("150 °C under nitrogen", 150), ("-20 °C", -20), ("−20 C", -20),
    ("20.5 °C", 20.5), ("125 °C", 125), ("+37℃", 37),
    (".5 °C", .5), ("298 K", 24.85), ("0 K", -273.15),
    ("reflux at 80 °C", 80), ("200 C in potassium solution", 200),
    ("80 °C for 2 h", 80),
])
def test_explicit_temperature_preserves_sign_decimal_and_unit(raw, expected):
    value, label = standardize_temperature(raw)
    assert value == pytest.approx(expected)
    assert label == f"{expected:g}°C"


@pytest.mark.parametrize("raw", [
    "room temperature", "RT", "ambient", "ice bath", "reflux", "heating",
    "elevated temperature", "body temperature", "cold", "start reaction",
    "80 °F", "80 Fahrenheit", "80", "20 cold", "1e2 °C", "-1 K",
    "-300 °C", "80 and 100 C", "80 C then 150 C", "80 C and 150 C",
    "RT then 80 C", "room temperature to 80 C", "80 C to room temperature",
    "<80 C", "≥80 C", "above 80 C", "up to 80 C", "80 ± 5 C",
    "80 +/- 5 C", "80 C/min", "80 C per minute", "1,000 C",
    "T <= 80 °C", "T >= 80 °C", "80 °C ≤ T", "80 °C >= T",
    "80 °C or above", "80 °C and lower", "80 °C and 100", "80 °C or 100",
    "80 °C, 100", "less than 80 C", "more than 80 C",
])
def test_qualitative_unsupported_or_ambiguous_temperature_is_label_only(raw):
    assert standardize_temperature(raw) == (None, raw)
    assert temperature_distribution([{"temperature": raw}]) == []
    assert temperature_labels([{"temperature": raw}]) == [
        {"label": raw, "count": 1, "percentage": 100.0}
    ]


@pytest.mark.parametrize("raw,label", [
    ("110–140 °C", "100-150°C"), ("-20.5 to -10.5 C", "≤0°C"),
    ("-20--10 C", "≤0°C"), ("10 C to 20 C", "0-30°C"),
    ("290–300 K", "0-30°C"), ("10 ℃–20 ℃", "0-30°C"),
])
def test_entire_interval_in_one_bin_counts_once_without_inventing_midpoint(raw, label):
    assert standardize_temperature(raw) == (None, raw)
    assert temperature_distribution([{"temperature": raw}]) == [
        {"label": label, "count": 1, "percentage": 100.0}
    ]


@pytest.mark.parametrize("raw", [
    "100-150 °C", "-20 to 20 C", "10–80 C", "150–100 C",
    "110-140 C then 200 C", "110-140 C and 200-220 C", "up to 110-140 C",
])
def test_cross_bin_reversed_and_multistage_ranges_do_not_enter_single_bin(raw):
    assert standardize_temperature(raw) == (None, raw)
    assert temperature_distribution([{"temperature": raw}]) == []


@pytest.mark.parametrize("raw,category", [
    ("0 C", "≤0°C"), ("0.01 C", "0-30°C"), ("30 C", "0-30°C"),
    ("30.01 C", "30-60°C"), ("60 C", "30-60°C"),
    ("60.01 C", "60-100°C"), ("100 C", "60-100°C"),
    ("100.01 C", "100-150°C"), ("150 C", "100-150°C"),
    ("150.01 C", "150-200°C"), ("200 C", "150-200°C"),
    ("200.01 C", "200-300°C"), ("300 C", "200-300°C"),
    ("300.01 C", ">300°C"),
])
def test_existing_bin_boundaries_are_preserved(raw, category):
    assert temperature_distribution([{"temperature": raw}])[0]["label"] == category


def test_response_statistics_count_only_supported_evidence_and_keep_original_rows():
    rows = [{"temperature": value} for value in [
        "150 °C under nitrogen", "110–140 °C", "-20 °C", "298 K",
        "reflux", "100-150 °C", "80 C then 150 C", None,
    ]]
    before = deepcopy(rows)
    response = OnlineKnowledgeSearchResponse(
        material="fixture", mode="synthesis", query_time_ms=0, totalPapers=8,
        max_papers=8, exampleUsed=False, stats={}, dataframe=rows,
        syntheses=build_syntheses(rows), temperatureDistribution=temperature_distribution(rows),
        tempLabels=temperature_labels(rows),
    ).model_dump(mode="json")
    assert response["temperatureDistribution"] == [
        {"label": "100-150°C", "count": 2, "percentage": 50.0},
        {"label": "≤0°C", "count": 1, "percentage": 25.0},
        {"label": "0-30°C", "count": 1, "percentage": 25.0},
    ]
    assert rows == before == response["dataframe"]
    assert response["syntheses"][0]["temperature"] == "150 °C under nitrogen"
    assert response["syntheses"][4]["temperature"] == "reflux"
    assert any(item["label"] == "reflux" for item in response["tempLabels"])


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_missing_temperature_has_no_numeric_or_label_statistic(raw):
    assert standardize_temperature(raw) == (None, None)
    assert temperature_distribution([{"temperature": raw}]) == []
    assert temperature_labels([{"temperature": raw}]) == []
