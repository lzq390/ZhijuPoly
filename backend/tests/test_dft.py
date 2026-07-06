from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.config import Settings
from app.routers.dft import get_dft_molecule, get_pca_sample


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
async def test_get_pca_sample_returns_dft_points(test_app: FastAPI) -> None:
    response = await get_pca_sample(make_request(test_app), limit=10)

    assert response.total == 2
    assert {point.mol_id for point in response.results} == {"000001_Conf01", "000002_Conf02"}
    first = next(point for point in response.results if point.mol_id == "000001_Conf01")
    assert first.x == 0.1


@pytest.mark.asyncio
async def test_get_dft_molecule_returns_coordinates_and_trace(test_app: FastAPI) -> None:
    response = await get_dft_molecule("000001_Conf01", make_request(test_app))

    assert response.mol_id == "000001_Conf01"
    assert response.final_step == 2
    assert response.coordinates == [(6, 0.0, 0.0, 0.0)]
    assert [point.step for point in response.trace] == [0, 1, 2]


def test_settings_rejects_sqlite_structured_data_backend() -> None:
    with pytest.raises(ValueError, match="STRUCTURED_DATA_BACKEND must be 'postgres'"):
        Settings(
            allowed_origins="http://localhost:5173",
            structured_data_backend="sqlite",
            model_enabled=False,
        )
