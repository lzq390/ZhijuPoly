from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from starlette.requests import Request

from app.config import PROJECT_ROOT
from app.main import health
from app.models import SmilesQueryRequest
from app.routers.predict import predict
from app.routers.query import get_polymer_detail, query_smiles


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
                match_mode="exact",
                similarity_threshold=0.7,
                top_k=10,
            ),
            request,
        )

    assert exc_info.value.status_code == 422
    assert "invalid smiles" in exc_info.value.detail


@pytest.mark.asyncio
async def test_query_smiles_exact_returns_grouped_result(test_app: FastAPI) -> None:
    request = make_request(test_app)

    response = await query_smiles(
        SmilesQueryRequest(
            smiles="OCC",
            match_mode="exact",
            similarity_threshold=0.7,
            top_k=10,
        ),
        request,
    )

    assert response.match_type == "exact"
    assert response.total == 1
    assert response.results[0].polymer_name == "polymer_a"
    assert response.results[0].similarity_score == 1.0
    assert response.results[0].properties.thermal[0].property_name == "Tg"
    assert response.results[0].properties.electrical[0].property_name == "Conductivity"


@pytest.mark.asyncio
async def test_query_smiles_similarity_returns_sorted_results(test_app: FastAPI) -> None:
    request = make_request(test_app)

    response = await query_smiles(
        SmilesQueryRequest(
            smiles="CCO",
            match_mode="similarity",
            similarity_threshold=0.3,
            top_k=2,
        ),
        request,
    )

    assert response.match_type == "similarity"
    assert response.total == 2
    assert response.results[0].polymer_name == "polymer_a"
    assert response.results[0].similarity_score == 1.0
    assert response.results[0].similarity_score >= response.results[1].similarity_score


@pytest.mark.asyncio
async def test_get_polymer_detail(test_app: FastAPI) -> None:
    request = make_request(test_app)

    response = await get_polymer_detail(1, request)

    assert response.polymer_id == "1"
    assert response.polymer_name == "polymer_a"
    assert response.properties.thermal[0].property_name == "Tg"


@pytest.mark.asyncio
async def test_predict_returns_not_implemented() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await predict()

    assert exc_info.value.status_code == 501
    assert exc_info.value.detail == "预测功能暂未启用,接口已预留"


def test_api_uses_temporary_database(test_app: FastAPI) -> None:
    db_path = Path(test_app.state.settings.sqlite_db_path)

    assert db_path.exists()
    assert db_path != PROJECT_ROOT / "backend" / "data" / "polyprop.db"
