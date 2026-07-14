from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from threading import Event

import httpx
import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.config import Settings
from app.main import create_app, health
from app.models import (
    ExperimentalProcessBrowseResponse,
    ExperimentalPropertyBrowseResponse,
    PredictRequest,
    PropertyFilterOptionsResponse,
    PropertyFilterSearchResponse,
    SmilesQueryRequest,
    Structure3DRequest,
)
from app.routers import database_browser
from app.routers.predict import predict
from app.postgres_database import postgres_connection
from app.routers.query import generate_structure_3d, get_polymer_detail, query_smiles, router as query_router
from app.services.database_browser import browse_csv_records
from app.services.gpu_runtime_registry import GpuRuntimeRegistry
from app.services.image_recognition import RecognizedStructure
from app.services.structure_3d import generate_3d_molblock
from app.utils.exceptions import StructureRecognitionError


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


REVERSE_DESIGN_DEMO_SMILES = (
    "*c1ccc(C(=O)OCc2ccc(-c3ccc(COC(=O)c4ccc(N5C(=O)c6ccc(OC(=O)c7ccc"
    "(C(C)c8ccc(C(=O)Oc9ccc%10c(c9)C(=O)N(*)C%10=O)o8)o7)cc6C5=O)cc4)o3)o2)cc1"
)
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 32)


async def post_structure_image(
    app: FastAPI,
    *,
    filename: str = "structure.png",
    content: bytes = PNG_BYTES,
    content_type: str = "image/png",
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post(
            "/api/v1/structure/recognize-image",
            files={"image": (filename, content, content_type)},
        )


def install_fake_ocsr_runtime(app: FastAPI, runtime: object | None = None) -> object:
    selected_runtime = runtime or object()
    registry = GpuRuntimeRegistry()
    registry.register("ocsr", enabled=True, loader=lambda: selected_runtime)
    app.state.gpu_runtime_registry = registry
    return selected_runtime


@pytest.mark.asyncio
async def test_health() -> None:
    assert await health() == {"status": "ok"}


def _insert_experimental_process_rows(app: FastAPI, source_file: str = "process.csv") -> None:
    with postgres_connection(app.state.settings.app_postgres_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO experimental.process_records (
                  source_file, source_row_number, polymer_id, polymer_name, product_name,
                  process_flow_original_text, material_original_text
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (source_file, 2, "1", "Poly A", "Film", "heated at 80 C", ""),
                    (source_file, 3, "2", "Poly B", "Aerogel", "washed with DMF and dried", "ODA material"),
                ],
            )


def _insert_experimental_property_rows(app: FastAPI, source_file: str = "properties.csv") -> None:
    with postgres_connection(app.state.settings.app_postgres_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO experimental.property_records (
                  source_file, source_row_number, polymer_id, polymer_name, property_category, property_name_en, value
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (source_file, 2, "1", "Poly A", None, "Tg", "120"),
                    (source_file, 3, "2", "Poly B", None, "thermal conductivity", "0.33"),
                    (source_file, 4, "3", "Poly C", None, "thermal conductivity", "0.44"),
                ],
            )


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
        "property_category": "Thermal",
        "property_name": "Tg",
        "property_value": "123.4",
        "property_value_num": 123.4,
        "property_unit": "°C",
        "label_source": "exp",
    }


def test_api_rejects_browser_cross_site_fetch_requests(test_app: FastAPI) -> None:
    client = TestClient(test_app)

    response = client.get(
        "/api/v1/database-browser/structure-property",
        headers={
            "Origin": "http://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "cross-site browser requests are not allowed"


def test_api_rejects_untrusted_browser_origin_without_fetch_metadata(test_app: FastAPI) -> None:
    client = TestClient(test_app)

    response = client.get(
        "/api/v1/database-browser/structure-property",
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "cross-site browser requests are not allowed"


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
    assert data["results"][0]["structure_svg"] is not None
    assert "<svg" in data["results"][0]["structure_svg"]
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


def test_smiles_lookup_searches_pi_candidate_table(test_app: FastAPI) -> None:
    client = TestClient(test_app)

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
    assert data["results"][0]["structure_svg"] is not None
    assert "<svg" in data["results"][0]["structure_svg"]
    assert data["results"][0]["fields"]["tg_celsius"] == 215.0

def test_settings_rejects_sqlite_runtime_backends() -> None:
    with pytest.raises(ValueError, match="STRUCTURED_DATA_BACKEND must be 'postgres'"):
        Settings(
            allowed_origins="http://localhost:5173",
            structured_data_backend="sqlite",
            pi_reverse_backend="postgres",
            model_enabled=False,
        )

    with pytest.raises(ValueError, match="PI_REVERSE_BACKEND must be 'postgres'"):
        Settings(
            allowed_origins="http://localhost:5173",
            structured_data_backend="postgres",
            pi_reverse_backend="sqlite",
            model_enabled=False,
        )

def test_dft_browser_lists_molecule_final_records(test_app: FastAPI) -> None:
    client = TestClient(test_app)

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


def test_dft_browser_searches_molecules_by_mol_id(test_app: FastAPI) -> None:
    client = TestClient(test_app)

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


def test_dft_browser_lists_and_filters_energy_steps(test_app: FastAPI) -> None:
    client = TestClient(test_app)

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


def test_dft_browser_filters_energy_steps_by_exact_mol_id(test_app: FastAPI) -> None:
    client = TestClient(test_app)

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
    assert routes["/api/v1/database-browser/property-filter/options"].response_model is PropertyFilterOptionsResponse
    assert routes["/api/v1/database-browser/property-filter/search"].response_model is PropertyFilterSearchResponse


def test_experimental_csv_routes_are_sync_for_threadpool() -> None:
    assert not inspect.iscoroutinefunction(database_browser.browse_experimental_process_records)
    assert not inspect.iscoroutinefunction(database_browser.browse_experimental_property_records)
    assert not inspect.iscoroutinefunction(database_browser.get_property_filter_options)
    assert not inspect.iscoroutinefunction(database_browser.search_property_filter)


def test_experimental_process_browser_endpoint_returns_typed_records(test_app: FastAPI) -> None:
    _insert_experimental_process_rows(test_app)

    response = TestClient(test_app).get(
        "/api/v1/database-browser/experimental-process",
        params={"q": "dmf", "page": 1, "page_size": 10},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "dmf"
    assert data["total_records"] == 2
    assert data["matched_records"] == 1
    assert data["data_source"] == "postgres"
    assert data["results"] == [
        {
            "source_file": "process.csv",
            "source_row_number": 3,
            "polymer_id": "2",
            "polymer_name": "Poly B",
            "product_name": "Aerogel",
            "process_flow_original_text": "washed with DMF and dried",
            "material_original_text": "ODA material",
        }
    ]

def test_experimental_property_browser_endpoint_returns_typed_records(test_app: FastAPI) -> None:
    _insert_experimental_property_rows(test_app)

    response = TestClient(test_app).get(
        "/api/v1/database-browser/experimental-property",
        params={"q": "thermal conductivity", "page": 2, "page_size": 1},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query"] == "thermal conductivity"
    assert data["total_records"] == 3
    assert data["matched_records"] == 2
    assert data["data_source"] == "postgres"
    assert data["results"] == [
        {
            "source_file": "properties.csv",
            "source_row_number": 4,
            "polymer_id": "3",
            "polymer_name": "Poly C",
            "property_name_en": "thermal conductivity",
            "property_category": None,
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
    assert response.results[0].polymer_name == "polymer_a"
    assert response.results[0].similarity_score == 1.0
    assert response.results[0].similarity_score >= response.results[1].similarity_score
    assert response.results[0].structure_svg is not None
    assert "<svg" in response.results[0].structure_svg
    assert any(item.property_name == "Tg" for item in response.results[0].properties.thermal)
    assert any(item.property_name == "Conductivity" for item in response.results[0].properties.electrical)


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
        any(item.property_name == "Glass transition temperature" for item in result.properties.thermal)
        for result in response.results
    )


@pytest.mark.asyncio
async def test_get_polymer_detail(test_app: FastAPI) -> None:
    request = make_request(test_app)

    response = await get_polymer_detail(1, request)

    assert response.polymer_id == "1"
    assert response.polymer_name == "polymer_a"
    assert any(item.property_name == "Tg" for item in response.properties.thermal)


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


def test_api_uses_temporary_postgres_database(test_app: FastAPI) -> None:
    assert "zhijupoly_test_" in test_app.state.settings.app_postgres_dsn
    assert test_app.state.settings.structured_data_backend == "postgres"


@pytest.mark.asyncio
async def test_generate_structure_3d_uses_configured_timeout(test_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    request = make_request(test_app)
    test_app.state.settings.structure_3d_timeout_seconds = 27.5
    captured: dict[str, float] = {}

    def fake_generate_3d_molblock(smiles: str, *, timeout_seconds: float) -> tuple[str, str]:
        captured["timeout_seconds"] = timeout_seconds
        return ("mock molblock V2000", smiles)

    monkeypatch.setattr("app.routers.query.generate_3d_molblock", fake_generate_3d_molblock)

    response = await generate_structure_3d(Structure3DRequest(smiles="*CC*"), request)

    assert response.format == "mol"
    assert captured == {"timeout_seconds": 27.5}


@pytest.mark.asyncio
async def test_recognize_structure_image_returns_molfile_first(
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    captured_runtimes: list[object | None] = []

    def fake_recognize(
        image_bytes: bytes,
        *,
        content_type: str | None,
        model_path: Path,
        device: str,
        max_bytes: int,
        runtime: object | None = None,
    ) -> RecognizedStructure:
        captured["image_bytes"] = image_bytes
        captured["content_type"] = content_type
        captured["model_path"] = model_path
        captured["device"] = device
        captured["max_bytes"] = max_bytes
        captured["runtime"] = runtime
        captured_runtimes.append(runtime)
        return RecognizedStructure(
            smiles="CCO",
            molfile="mock molfile V2000",
            confidence=0.91,
            warnings=["low confidence"],
        )

    monkeypatch.setattr("app.routers.query.recognize_structure_image_from_bytes", fake_recognize)
    test_app.state.settings.ocsr_enabled = True
    test_app.state.settings.ocsr_max_image_bytes = 1024

    class ObservingRegistry(GpuRuntimeRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.active_when_success_recorded: list[int] = []

        def record_inference_success(self, name: str) -> None:
            self.active_when_success_recorded.append(self.active_inferences)
            super().record_inference_success(name)

    expected_runtime = object()
    load_calls = 0

    def load_runtime() -> object:
        nonlocal load_calls
        load_calls += 1
        return expected_runtime

    registry = ObservingRegistry()
    registry.register("ocsr", enabled=True, loader=load_runtime)
    registry.preload_enabled()
    test_app.state.gpu_runtime_registry = registry

    response = await post_structure_image(test_app)
    second_response = await post_structure_image(test_app)

    assert response.status_code == 200
    assert second_response.status_code == 200
    data = response.json()
    assert data["smiles"] == "CCO"
    assert data["molfile"] == "mock molfile V2000"
    assert data["confidence"] == 0.91
    assert data["warnings"] == ["low confidence"]
    assert data["query_time_ms"] >= 0
    assert captured["image_bytes"] == PNG_BYTES
    assert captured["content_type"] == "image/png"
    assert captured["max_bytes"] == 1024
    assert captured["runtime"] is expected_runtime
    assert captured_runtimes == [expected_runtime, expected_runtime]
    assert load_calls == 1
    assert registry.active_when_success_recorded == [1, 1]


@pytest.mark.asyncio
async def test_recognize_structure_image_does_not_block_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inference_started = Event()
    release_inference = Event()

    def blocking_recognize(*_args: object, **_kwargs: object) -> RecognizedStructure:
        inference_started.set()
        assert release_inference.wait(timeout=3)
        return RecognizedStructure(smiles="CCO", molfile="mock molfile V2000", confidence=0.9)

    monkeypatch.setattr("app.routers.query.recognize_structure_image_from_bytes", blocking_recognize)
    test_app = FastAPI()
    test_app.state.settings = Settings(
        sqlite_db_path=str(tmp_path / "polyprop.db"),
        csv_source_path=str(tmp_path / "source.csv"),
        model_enabled=False,
        ocsr_enabled=True,
    )
    install_fake_ocsr_runtime(test_app)
    test_app.include_router(query_router)
    test_app.add_api_route("/health", health, methods=["GET"])

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        inference_task = asyncio.create_task(
            client.post(
                "/api/v1/structure/recognize-image",
                files={"image": ("structure.png", PNG_BYTES, "image/png")},
            )
        )
        try:
            assert await asyncio.to_thread(inference_started.wait, 2)
            health_response = await asyncio.wait_for(client.get("/health"), timeout=0.5)
            assert health_response.status_code == 200
        finally:
            release_inference.set()
        inference_response = await asyncio.wait_for(inference_task, timeout=2)

    assert inference_response.status_code == 200


@pytest.mark.asyncio
async def test_recognize_structure_image_respects_disabled_flag(test_app: FastAPI) -> None:
    test_app.state.settings.ocsr_enabled = False

    response = await post_structure_image(test_app)

    assert response.status_code == 503
    assert response.json()["detail"] == "image recognition service is disabled"


@pytest.mark.asyncio
async def test_recognize_structure_image_reports_missing_model(test_app: FastAPI, tmp_path: Path) -> None:
    test_app.state.settings.ocsr_enabled = True
    test_app.state.settings.ocsr_model_dir = str(tmp_path / "missing-ocsr-model")

    response = await post_structure_image(test_app)

    assert response.status_code == 503
    assert "OCSR model path not found" in response.json()["detail"]


@pytest.mark.asyncio
async def test_recognize_structure_image_rejects_unsupported_file_type(test_app: FastAPI) -> None:
    test_app.state.settings.ocsr_enabled = True

    response = await post_structure_image(
        test_app,
        filename="structure.txt",
        content=b"not image",
        content_type="text/plain",
    )

    assert response.status_code == 415
    assert response.json()["detail"] == "unsupported image type"


@pytest.mark.asyncio
async def test_recognize_structure_image_rejects_oversized_file(test_app: FastAPI) -> None:
    test_app.state.settings.ocsr_enabled = True
    test_app.state.settings.ocsr_max_image_bytes = 8

    response = await post_structure_image(test_app)

    assert response.status_code == 413
    assert response.json()["detail"] == "image file is too large"


@pytest.mark.parametrize(
    ("message", "expected_detail"),
    [
        ("recognition did not return a structure", "recognition did not return a structure"),
        (
            "recognition result is not a valid chemical structure",
            "recognition result is not a valid chemical structure",
        ),
    ],
)
@pytest.mark.asyncio
async def test_recognize_structure_image_reports_unusable_results(
    message: str,
    expected_detail: str,
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_recognize(*_args: object, **_kwargs: object) -> RecognizedStructure:
        raise StructureRecognitionError(message)

    monkeypatch.setattr("app.routers.query.recognize_structure_image_from_bytes", fake_recognize)
    test_app.state.settings.ocsr_enabled = True
    install_fake_ocsr_runtime(test_app)

    response = await post_structure_image(test_app)

    assert response.status_code == 422
    assert response.json()["detail"] == expected_detail


@pytest.mark.asyncio
async def test_generate_structure_3d_caps_polymer_ends(test_app: FastAPI) -> None:
    response = await generate_structure_3d(Structure3DRequest(smiles="*CC*"), make_request(test_app))

    assert response.format == "mol"
    assert "V2000" in response.molblock
    assert " C " in response.molblock or " H " in response.molblock
    assert "[H]" in response.capped_smiles


@pytest.mark.asyncio
async def test_generate_structure_3d_rejects_invalid_smiles(test_app: FastAPI) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await generate_structure_3d(Structure3DRequest(smiles="not-a-smiles"), make_request(test_app))

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("smiles", ["*=C*", "*#C*", "*1CC1*"])
async def test_generate_structure_3d_rejects_invalid_capped_topology(smiles: str, test_app: FastAPI) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await generate_structure_3d(Structure3DRequest(smiles=smiles), make_request(test_app))

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_generate_structure_3d_handles_complex_polymer_structure(test_app: FastAPI) -> None:
    smiles = "*/C=C\\C(=O)c1cccc(c1)C(=O)/C=C\\Nc1ccc(cc1)Cc1ccc(cc1)N*"
    response = await generate_structure_3d(Structure3DRequest(smiles=smiles), make_request(test_app))

    assert response.format == "mol"
    assert "V2000" in response.molblock
    assert "[H]" in response.capped_smiles


@pytest.mark.asyncio
async def test_generate_structure_3d_handles_large_reverse_design_preview(test_app: FastAPI) -> None:
    response = await generate_structure_3d(Structure3DRequest(smiles=REVERSE_DESIGN_DEMO_SMILES), make_request(test_app))

    assert response.format == "mol"
    assert "V2000" in response.molblock
    assert len(response.molblock) > 8000


def test_generate_3d_molblock_handles_large_payload_without_queue_deadlock() -> None:
    molblock, capped_smiles = generate_3d_molblock(REVERSE_DESIGN_DEMO_SMILES)

    assert "V2000" in molblock
    assert len(molblock) > 8000
    assert capped_smiles.startswith("[H]c1ccc")
