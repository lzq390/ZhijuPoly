from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import (
    PredictRequest,
    PredictResponse,
    PolymerResult,
    PropertyGroups,
    PropertyItem,
    SmilesQueryRequest,
    SmilesQueryResponse,
    Structure3DRequest,
    Structure3DResponse,
)


def test_smiles_query_request_defaults() -> None:
    request = SmilesQueryRequest(smiles=" CCO ")

    assert request.smiles == "CCO"
    assert request.match_mode == "exact"
    assert request.similarity_threshold == 0.7
    assert request.top_k == 10


def test_smiles_query_request_rejects_invalid_values() -> None:
    with pytest.raises(ValidationError):
        SmilesQueryRequest(smiles=" ")

    with pytest.raises(ValidationError):
        SmilesQueryRequest(smiles="CCO", similarity_threshold=1.5)

    with pytest.raises(ValidationError):
        SmilesQueryRequest(smiles="CCO", top_k=0)


def test_property_groups_default_to_empty_lists() -> None:
    groups = PropertyGroups()

    assert groups.thermal == []
    assert groups.mechanical == []
    assert groups.electrical == []
    assert groups.chemical == []
    assert groups.optical == []
    assert groups.other == []


def test_query_response_serializes_polymer_results() -> None:
    item = PropertyItem(
        property_category="Electrical",
        property_name="Electric conductivity",
        property_value="1.00E-10",
        property_value_num=1e-10,
        property_unit="1/(ohm*cm)",
        label_source="exp",
    )
    result = PolymerResult(
        polymer_id="1",
        polymer_name="example polymer",
        smiles="*CC*",
        canonical_smiles="*CC*",
        similarity_score=1.0,
        properties=PropertyGroups(electrical=[item]),
    )
    response = SmilesQueryResponse(
        match_type="exact",
        query_time_ms=12.5,
        total=1,
        results=[result],
    )

    payload = response.model_dump()

    assert payload["match_type"] == "exact"
    assert payload["total"] == 1
    assert payload["results"][0]["properties"]["electrical"][0]["property_name"] == "Electric conductivity"


def test_structure_3d_models_validate() -> None:
    request = Structure3DRequest(smiles=" *CC* ")
    response = Structure3DResponse(molblock="mol", capped_smiles="[H]CC[H]", format="mol")

    assert request.smiles == "*CC*"
    assert response.format == "mol"


def test_predict_models_validate() -> None:
    request = PredictRequest(
        smiles=" CCO ",
        properties=["Glass transition temperature"],
    )
    response = PredictResponse(
        predictions={"Glass transition temperature": 123.4},
        query_time_ms=12.5,
    )

    assert request.smiles == "CCO"
    assert request.properties == ["Glass transition temperature"]
    assert response.predictions["Glass transition temperature"] == 123.4


def test_predict_request_rejects_invalid_properties() -> None:
    with pytest.raises(ValidationError):
        PredictRequest(smiles="CCO", properties=[])

    request = PredictRequest(smiles="CCO", properties=["Tensile stress strength at break"])
    assert request.properties == ["Tensile stress strength at break"]

    request = PredictRequest(smiles="CCO", properties=["  Glass transition temperature  "])
    assert request.properties == ["Glass transition temperature"]

    with pytest.raises(ValidationError):
        PredictRequest(smiles="CCO", properties=[" "])
