from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.config import PROJECT_ROOT, Settings
from app.database import rebuild_fumol_schema, sqlite_connection
from app.main import create_app, health
from app.models import (
    ExperimentalProcessBrowseResponse,
    ExperimentalPropertyBrowseResponse,
    PredictRequest,
    SmilesQueryRequest,
    Structure3DRequest,
)
from app.pi_database import ensure_pi_schema
from app.routers import database_browser
from app.routers.predict import predict
from app.routers.query import generate_structure_3d, get_polymer_detail, query_smiles
from app.services.database_browser import browse_csv_records


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


def test_structure_property_browser_lists_records_with_total_count(test_app: FastAPI) -> None:
    client = TestClient(test_app)

    response = client.get("/api/v1/database-browser/structure-property", params={"page": 1, "page_size": 2})

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == ""
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert data["total_records"] == 6
    assert data["matched_records"] == 6
    assert len(data["results"]) == 2
    assert data["results"][0] == {
        "property_id": 1,
        "polymer_id": 1,
        "smiles": "CCO",
        "canonical_smiles": "CCO",
        "property_name": "Tg",
        "property_value": "123.4",
        "property_value_num": 123.4,
        "property_unit": "°C",
        "label_source": "exp",
    }


def test_structure_property_browser_searches_properties_and_smiles(test_app: FastAPI) -> None:
    client = TestClient(test_app)

    property_response = client.get(
        "/api/v1/database-browser/structure-property",
        params={"q": "Glass transition", "page": 1, "page_size": 10},
    )
    assert property_response.status_code == 200
    property_data = property_response.json()
    assert property_data["total_records"] == 6
    assert property_data["matched_records"] == 2
    assert [row["property_name"] for row in property_data["results"]] == [
        "Glass transition temperature",
        "Glass transition temperature",
    ]

    smiles_response = client.get(
        "/api/v1/database-browser/structure-property",
        params={"q": "CCO", "page": 1, "page_size": 10},
    )
    assert smiles_response.status_code == 200
    smiles_data = smiles_response.json()
    assert smiles_data["matched_records"] == 3
    assert {row["property_name"] for row in smiles_data["results"]} == {
        "Tg",
        "Glass transition temperature",
        "Conductivity",
    }


def test_smiles_lookup_finds_canonical_match_in_polymers(test_app: FastAPI) -> None:
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/database-browser/smiles-lookup",
        json={"smiles": "OCC", "table": "polymers"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query_smiles"] == "OCC"
    assert data["canonical_smiles"] == "CCO"
    assert data["table"] == "polymers"
    assert data["exists"] is True
    assert data["total"] == 1
    assert data["results"][0]["record_id"] == "1"
    assert data["results"][0]["source_column"] == "canonical_smiles"
    assert data["results"][0]["smiles"] == "CCO"
    assert data["results"][0]["canonical_smiles"] == "CCO"
    assert data["results"][0]["fields"]["property_count"] == 3


def test_smiles_lookup_finds_property_rows_for_selected_smiles(test_app: FastAPI) -> None:
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/database-browser/smiles-lookup",
        json={"smiles": "OCC", "table": "properties"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["table"] == "properties"
    assert data["exists"] is True
    assert data["total"] == 3
    assert {row["summary"] for row in data["results"]} == {
        "Tg",
        "Glass transition temperature",
        "Conductivity",
    }
    assert all(row["fields"]["polymer_id"] == 1 for row in data["results"])


def test_smiles_lookup_rejects_invalid_smiles(test_app: FastAPI) -> None:
    client = TestClient(test_app)

    response = client.post(
        "/api/v1/database-browser/smiles-lookup",
        json={"smiles": "not-a-smiles", "table": "polymers"},
    )

    assert response.status_code == 422
    assert "invalid smiles" in response.json()["detail"]


def test_smiles_lookup_searches_pi_candidate_table(tmp_path: Path) -> None:
    sqlite_db_path = tmp_path / "polyprop.db"
    pi_db_path = tmp_path / "pi_reverse.db"

    with sqlite_connection(pi_db_path) as connection:
        ensure_pi_schema(connection)
        connection.execute(
            """
            INSERT INTO pi_candidates (
              pi_id, mon1, mon2, polym, canonical_polym, rdkit_parse_ok,
              tg_celsius, dielectric_const_dc, static_dielectric_const,
              dipole_debye, electrophilicity_index, homo_lumo_gap_ev,
              hardness, mulliken_electronegativity, redox_window_v,
              linear_expansion, refractive_index, morgan_fp, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                7,
                "NCCN",
                "O=C=O",
                "CCO",
                "CCO",
                1,
                215.0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "2026-05-15",
            ),
        )

    settings = Settings(
        sqlite_db_path=str(sqlite_db_path),
        pi_reverse_db_path=str(pi_db_path),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
    )
    client = TestClient(create_app(settings))

    response = client.post(
        "/api/v1/database-browser/smiles-lookup",
        json={"smiles": "OCC", "table": "pi_candidates"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["table"] == "pi_candidates"
    assert data["exists"] is True
    assert data["total"] == 1
    assert data["results"][0]["record_id"] == "7"
    assert data["results"][0]["source_column"] == "canonical_polym"
    assert data["results"][0]["fields"]["tg_celsius"] == 215.0


def test_smiles_lookup_reports_missing_pi_candidate_database(tmp_path: Path) -> None:
    sqlite_db_path = tmp_path / "polyprop.db"
    missing_pi_db_path = tmp_path / "missing_pi_reverse.db"

    settings = Settings(
        sqlite_db_path=str(sqlite_db_path),
        pi_reverse_db_path=str(missing_pi_db_path),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
    )
    client = TestClient(create_app(settings))

    response = client.post(
        "/api/v1/database-browser/smiles-lookup",
        json={"smiles": "OCC", "table": "pi_candidates"},
    )

    assert response.status_code == 503
    assert "PI reverse-design database not found" in response.json()["detail"]


def make_dft_browser_app(tmp_path: Path) -> FastAPI:
    sqlite_db_path = tmp_path / "polyprop.db"
    fumol_db_path = tmp_path / "fumol.db"

    with sqlite_connection(fumol_db_path) as connection:
        rebuild_fumol_schema(connection)
        connection.executemany(
            """
            INSERT INTO dft_molecule_final (
              mol_id, range_group, final_step, n_atoms, coordinates, scf_energy,
              zero_point_energy, thermal_enthalpy, gibbs_free_energy, lowest_freq,
              dipole_moment, homo_ev, lumo_ev, gap_ev, is_converged, pca_x, pca_y, pca_z
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "000001_Conf01",
                    "small",
                    2,
                    12,
                    "[[6, 0, 0, 0]]",
                    -100.1,
                    -99.0,
                    -98.5,
                    -98.2,
                    12.5,
                    1.2,
                    -6.1,
                    94.1,
                    100.2,
                    "44",
                    0.1,
                    0.2,
                    0.3,
                ),
                (
                    "000002_Conf02",
                    "large",
                    1,
                    24,
                    "[[8, 0, 0, 0]]",
                    -200.2,
                    -198.0,
                    -197.5,
                    -197.2,
                    8.5,
                    2.4,
                    -7.2,
                    95.3,
                    102.5,
                    "34",
                    1.1,
                    1.2,
                    1.3,
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO dft_energy_trace (
              mol_id, step, scf_energy, homo_ev, lumo_ev, gap_ev
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("000001_Conf01", 0, -99.5, -6.0, 93.8, 99.8),
                ("000001_Conf01", 1, -100.0, -6.05, 94.0, 100.05),
                ("000001_Conf01", 2, -100.1, -6.1, 94.1, 100.2),
                ("000002_Conf02", 0, -199.9, -7.0, 95.0, 102.0),
                ("000002_Conf02", 1, -200.2, -7.2, 95.3, 102.5),
            ],
        )

    settings = Settings(
        sqlite_db_path=str(sqlite_db_path),
        fumol_db_path=str(fumol_db_path),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
    )
    return create_app(settings)


def test_dft_browser_lists_molecule_final_records(tmp_path: Path) -> None:
    client = TestClient(make_dft_browser_app(tmp_path))

    response = client.get("/api/v1/database-browser/dft/molecules", params={"page": 1, "page_size": 1})

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == ""
    assert data["page"] == 1
    assert data["page_size"] == 1
    assert data["total_records"] == 2
    assert data["matched_records"] == 2
    assert data["total_step_records"] == 5
    assert data["average_steps"] == 2.5
    assert data["max_steps"] == 3
    assert data["results"][0]["mol_id"] == "000001_Conf01"
    assert data["results"][0]["trace_points"] == 3
    assert data["results"][0]["final_step"] == 2
    assert data["results"][0]["gap_ev"] == 100.2


def test_dft_browser_searches_molecules_by_mol_id(tmp_path: Path) -> None:
    client = TestClient(make_dft_browser_app(tmp_path))

    response = client.get(
        "/api/v1/database-browser/dft/molecules",
        params={"q": "Conf02", "page": 1, "page_size": 10},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total_records"] == 2
    assert data["matched_records"] == 1
    assert data["results"][0]["mol_id"] == "000002_Conf02"
    assert data["results"][0]["range_group"] == "large"


def test_dft_browser_lists_and_filters_energy_steps(tmp_path: Path) -> None:
    client = TestClient(make_dft_browser_app(tmp_path))

    response = client.get(
        "/api/v1/database-browser/dft/steps",
        params={"q": "000001_Conf01", "page": 1, "page_size": 2},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "000001_Conf01"
    assert data["total_records"] == 5
    assert data["matched_records"] == 3
    assert data["results"] == [
        {
            "mol_id": "000001_Conf01",
            "step": 0,
            "scf_energy": -99.5,
            "homo_ev": -6.0,
            "lumo_ev": 93.8,
            "gap_ev": 99.8,
        },
        {
            "mol_id": "000001_Conf01",
            "step": 1,
            "scf_energy": -100.0,
            "homo_ev": -6.05,
            "lumo_ev": 94.0,
            "gap_ev": 100.05,
        },
    ]


def test_dft_browser_filters_energy_steps_by_exact_mol_id(tmp_path: Path) -> None:
    client = TestClient(make_dft_browser_app(tmp_path))

    response = client.get(
        "/api/v1/database-browser/dft/steps",
        params={"mol_id": "000001_Conf01", "q": "ignored", "page": 1, "page_size": 10},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "000001_Conf01"
    assert data["total_records"] == 5
    assert data["matched_records"] == 3
    assert [row["step"] for row in data["results"]] == [0, 1, 2]
    assert {row["mol_id"] for row in data["results"]} == {"000001_Conf01"}


def test_csv_browser_returns_paginated_property_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "properties.csv"
    csv_path.write_text(
        "\n".join(
            [
                "polymer_id,polymer_name,property_name_en,value",
                "1,Poly A,Tg,120",
                "2,Poly B,thermal conductivity,0.33",
                "3,Poly C,thermal conductivity,0.44",
            ]
        ),
        encoding="utf-8",
    )

    total, matched, rows = browse_csv_records(
        csv_path,
        source_file="properties.csv",
        query="thermal conductivity",
        page=1,
        page_size=1,
    )

    assert total == 3
    assert matched == 2
    assert len(rows) == 1
    assert rows[0].source_row_number == 3
    assert rows[0].data["polymer_name"] == "Poly B"


def test_csv_browser_searches_long_process_text(tmp_path: Path) -> None:
    csv_path = tmp_path / "process.csv"
    csv_path.write_text(
        "\n".join(
            [
                "polymer_id,polymer_name,product_name,process_flow_original_text,material_original_text",
                '1,Poly A,Film,"heated at 80 C",""',
                '2,Poly B,Aerogel,"washed with DMF and dried","ODA material"',
            ]
        ),
        encoding="utf-8",
    )

    total, matched, rows = browse_csv_records(
        csv_path,
        source_file="process.csv",
        query="dmf",
        page=1,
        page_size=10,
    )

    assert total == 2
    assert matched == 1
    assert rows[0].source_row_number == 3
    assert rows[0].data["product_name"] == "Aerogel"


def test_experimental_csv_routes_keep_response_models() -> None:
    routes = {route.path: route for route in database_browser.router.routes}

    assert routes["/api/v1/database-browser/experimental-process"].response_model is ExperimentalProcessBrowseResponse
    assert routes["/api/v1/database-browser/experimental-property"].response_model is ExperimentalPropertyBrowseResponse


def test_experimental_csv_routes_are_sync_for_threadpool() -> None:
    assert not inspect.iscoroutinefunction(database_browser.browse_experimental_process_records)
    assert not inspect.iscoroutinefunction(database_browser.browse_experimental_property_records)


def test_experimental_process_browser_endpoint_returns_typed_records(
    monkeypatch: pytest.MonkeyPatch,
    test_app: FastAPI,
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "process.csv"
    csv_path.write_text(
        "\n".join(
            [
                "polymer_id,polymer_name,product_name,process_flow_original_text,material_original_text",
                '1,Poly A,Film,"heated at 80 C",""',
                '2,Poly B,Aerogel,"washed with DMF and dried","ODA material"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(database_browser, "EXPERIMENTAL_PROCESS_CSV", csv_path)

    response = TestClient(test_app).get(
        "/api/v1/database-browser/experimental-process",
        params={"q": "dmf", "page": 1, "page_size": 10},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "dmf"
    assert data["total_records"] == 2
    assert data["matched_records"] == 1
    assert data["results"] == [
        {
            "source_file": str(csv_path),
            "source_row_number": 3,
            "polymer_id": "2",
            "polymer_name": "Poly B",
            "product_name": "Aerogel",
            "process_flow_original_text": "washed with DMF and dried",
            "material_original_text": "ODA material",
        }
    ]


def test_experimental_property_browser_endpoint_returns_typed_records(
    monkeypatch: pytest.MonkeyPatch,
    test_app: FastAPI,
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "properties.csv"
    csv_path.write_text(
        "\n".join(
            [
                "polymer_id,polymer_name,property_name_en,value",
                "1,Poly A,Tg,120",
                "2,Poly B,thermal conductivity,0.33",
                "3,Poly C,thermal conductivity,0.44",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(database_browser, "EXPERIMENTAL_PROPERTY_CSV", csv_path)

    response = TestClient(test_app).get(
        "/api/v1/database-browser/experimental-property",
        params={"q": "thermal conductivity", "page": 2, "page_size": 1},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "thermal conductivity"
    assert data["total_records"] == 3
    assert data["matched_records"] == 2
    assert data["results"] == [
        {
            "source_file": str(csv_path),
            "source_row_number": 4,
            "polymer_id": "3",
            "polymer_name": "Poly C",
            "property_name_en": "thermal conductivity",
            "value": "0.44",
        }
    ]


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
