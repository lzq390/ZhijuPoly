from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.config import Settings
from app.database import rebuild_fumol_schema, sqlite_connection
from app.main import create_app
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


def write_fumol_fixture(db_path: Path) -> None:
    with sqlite_connection(db_path) as connection:
        rebuild_fumol_schema(connection)
        connection.execute(
            """
            INSERT INTO dft_molecule_final (
              mol_id, range_group, final_step, n_atoms, coordinates, scf_energy,
              zero_point_energy, thermal_enthalpy, gibbs_free_energy, lowest_freq,
              dipole_moment, homo_ev, lumo_ev, gap_ev, is_converged, pca_x, pca_y, pca_z
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "mol_a",
                "000000-099999",
                2,
                2,
                "[[6, 0.0, 0.0, 0.0], [1, 1.0, 0.0, 0.0]]",
                -1.23,
                -1.1,
                -1.0,
                -1.4,
                12.5,
                0.8,
                -7.1,
                101.2,
                108.3,
                "44",
                0.1,
                0.2,
                0.3,
            ),
        )
        connection.executemany(
            """
            INSERT INTO dft_energy_trace (mol_id, step, scf_energy, homo_ev, lumo_ev, gap_ev)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("mol_a", 1, -1.0, -7.0, 101.0, 108.0),
                ("mol_a", 2, -1.23, -7.1, 101.2, 108.3),
            ],
        )


@pytest.fixture
def dft_app(tmp_path: Path) -> FastAPI:
    db_path = tmp_path / "fumol.db"
    write_fumol_fixture(db_path)
    settings = Settings(
        sqlite_db_path=str(tmp_path / "polyprop.db"),
        csv_source_path=str(tmp_path / "data.csv"),
        fumol_db_path=str(db_path),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
    )
    return create_app(settings)


@pytest.mark.asyncio
async def test_get_pca_sample_returns_dft_points(dft_app: FastAPI) -> None:
    response = await get_pca_sample(make_request(dft_app), limit=10)

    assert response.total == 1
    assert len(response.results) == 1
    assert response.results[0].mol_id == "mol_a"
    assert response.results[0].x == 0.1


@pytest.mark.asyncio
async def test_get_dft_molecule_returns_coordinates_and_trace(dft_app: FastAPI) -> None:
    response = await get_dft_molecule("mol_a", make_request(dft_app))

    assert response.mol_id == "mol_a"
    assert response.final_step == 2
    assert response.coordinates == [(6, 0.0, 0.0, 0.0), (1, 1.0, 0.0, 0.0)]
    assert [point.step for point in response.trace] == [1, 2]
