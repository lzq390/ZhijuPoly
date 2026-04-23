from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.requests import Request

from app.config import PROJECT_ROOT
from app.main import health
from app.models import PredictRequest, SmilesQueryRequest, Structure3DRequest
from app.routers.predict import predict
from app.routers.query import generate_structure_3d, get_polymer_detail, query_smiles


def make_request(app: FastAPI) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "app": app,
        }
    )


@pytest.mark.asyncio
async def test_health() -> None:
    assert await health() == {"status": "ok"}


@pytest.mark.asyncio
async def test_query_smiles_rejects_invalid_input(test_app: FastAPI) -> None:
    request = make_request(test_app)

    with pytest.raises(HTTPException) as exc_info:
        await query_smiles(
            SmilesQueryRequest(
                smiles="not-a-smiles",
                match_mode="structure",
                similarity_threshold=0.7,
                top_k=10,
            ),
            request,
        )

    assert exc_info.value.status_code == 422
    assert "invalid smiles" in exc_info.value.detail


@pytest.mark.asyncio
async def test_query_smiles_structure_returns_top_structural_matches(test_app: FastAPI) -> None:
    request = make_request(test_app)

    response = await query_smiles(
        SmilesQueryRequest(
            smiles="OCC",
            match_mode="structure",
            similarity_threshold=0.7,
            top_k=10,
        ),
        request,
    )

    assert response.match_type == "structure"
    assert response.total == 2
    assert response.results[0].polymer_name == ""
    assert response.results[0].similarity_score == 1.0
    assert response.results[0].similarity_score >= response.results[1].similarity_score
    assert response.results[0].structure_svg is not None
    assert "<svg" in response.results[0].structure_svg
    assert any(item.property_name == "Tg" for item in response.results[0].properties.other)
    assert any(item.property_name == "Conductivity" for item in response.results[0].properties.other)


@pytest.mark.asyncio
async def test_query_smiles_property_requires_selected_property(test_app: FastAPI) -> None:
    request = make_request(test_app)

    with pytest.raises(HTTPException) as exc_info:
        await query_smiles(
            SmilesQueryRequest(
                smiles="CCO",
                match_mode="property",
                similarity_threshold=0.3,
                top_k=2,
            ),
            request,
        )

    assert exc_info.value.status_code == 422
    assert "property_name is required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_query_smiles_property_returns_nearest_property_matches(predict_enabled_app: FastAPI) -> None:
    request = make_request(predict_enabled_app)

    response = await query_smiles(
        SmilesQueryRequest(
            smiles="CCO",
            match_mode="property",
            similarity_threshold=0.3,
            top_k=2,
            property_name="Glass transition temperature",
        ),
        request,
    )

    assert response.match_type == "property"
    assert response.total == 2
    assert response.predicted_property_name == "Glass transition temperature"
    assert response.predicted_property_value is not None
    assert response.predicted_property_unit == "°C"
    assert response.results[0].similarity_score >= response.results[1].similarity_score
    assert response.results[0].structure_svg is not None
    assert response.results[0].matched_property_name == "Glass transition temperature"
    assert response.results[0].matched_property_value is not None
    assert response.results[0].matched_property_unit == "°C"
    assert response.results[0].matched_property_source in {"exp", "calc"}
    assert all(
        any(item.property_name == "Glass transition temperature" for item in result.properties.other)
        for result in response.results
    )


@pytest.mark.asyncio
async def test_get_polymer_detail(test_app: FastAPI) -> None:
    request = make_request(test_app)

    response = await get_polymer_detail(1, request)

    assert response.polymer_id == "1"
    assert response.polymer_name == ""
    assert any(item.property_name == "Tg" for item in response.properties.other)


@pytest.mark.asyncio
async def test_predict_returns_predictions(predict_enabled_app: FastAPI) -> None:
    request = make_request(predict_enabled_app)
    response = await predict(
        PredictRequest(
            smiles="CCO",
            properties=["Glass transition temperature"],
        ),
        request,
    )

    assert "Glass transition temperature" in response.predictions
    assert isinstance(response.predictions["Glass transition temperature"], float)
    assert response.query_time_ms >= 0.0


@pytest.mark.asyncio
async def test_predict_rejects_invalid_smiles(predict_enabled_app: FastAPI) -> None:
    request = make_request(predict_enabled_app)
    with pytest.raises(HTTPException) as exc_info:
        await predict(
            PredictRequest(
                smiles="not-a-smiles",
                properties=["Glass transition temperature"],
            ),
            request,
        )

    assert exc_info.value.status_code == 422
    assert "invalid smiles" in exc_info.value.detail


def test_predict_request_accepts_tensile_stress_strength_property() -> None:
    payload = PredictRequest(
        smiles="CCO",
        properties=["Tensile stress strength at break"],
    )

    assert payload.properties == ["Tensile stress strength at break"]


@pytest.mark.asyncio
async def test_predict_respects_model_enabled_flag(test_app: FastAPI) -> None:
    request = make_request(test_app)

    with pytest.raises(HTTPException) as exc_info:
        await predict(
            PredictRequest(
                smiles="CCO",
                properties=["Glass transition temperature"],
            ),
            request,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "prediction service is disabled"


@pytest.mark.asyncio
async def test_predict_uses_app_level_model_dir(predict_enabled_app: FastAPI) -> None:
    request = make_request(predict_enabled_app)

    response = await predict(
        PredictRequest(
            smiles="CCO",
            properties=["Tensile stress strength at break"],
        ),
        request,
    )

    assert "Tensile stress strength at break" in response.predictions


def test_api_uses_temporary_database(test_app: FastAPI) -> None:
    db_path = Path(test_app.state.settings.sqlite_db_path)

    assert db_path.exists()
    assert db_path != PROJECT_ROOT / "backend" / "data" / "polyprop.db"


@pytest.mark.asyncio
async def test_generate_structure_3d_caps_polymer_ends() -> None:
    response = await generate_structure_3d(Structure3DRequest(smiles="*CC*"))

    assert response.format == "mol"
    assert "V2000" in response.molblock
    assert " C " in response.molblock or " H " in response.molblock
    assert "[H]" in response.capped_smiles


@pytest.mark.asyncio
async def test_generate_structure_3d_rejects_invalid_smiles() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await generate_structure_3d(Structure3DRequest(smiles="not-a-smiles"))

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("smiles", ["*=C*", "*#C*", "*1CC1*"])
async def test_generate_structure_3d_rejects_invalid_capped_topology(smiles: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await generate_structure_3d(Structure3DRequest(smiles=smiles))

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_generate_structure_3d_handles_complex_polymer_structure() -> None:
    smiles = "*/C=C\\C(=O)c1cccc(c1)C(=O)/C=C\\Nc1ccc(cc1)Cc1ccc(cc1)N*"
    response = await generate_structure_3d(Structure3DRequest(smiles=smiles))

    assert response.format == "mol"
    assert "V2000" in response.molblock
    assert "[H]" in response.capped_smiles
