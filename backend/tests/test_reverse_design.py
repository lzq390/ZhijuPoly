from __future__ import annotations

from pathlib import Path
from time import sleep

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.config import Settings
from app.database import sqlite_connection
from app.import_pi_candidates import import_pi_candidates_to_sqlite
from app.main import create_app
from app.models import ReverseDesignTgRequest, ReverseDesignTgResponse
from app.routers.reverse_design import _search_by_tg_response, search_by_tg
from app.services.fingerprint import fingerprint_to_bytes, generate, tanimoto
from app.services.postgres_reverse_design import (
    tanimoto_fingerprint_bytes,
    search_reverse_design_by_tg_postgres,
)
from app.services.reverse_design_jobs import ReverseDesignJobManager
from app.services.reverse_design import search_reverse_design_by_tg


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


def write_pi_csv(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "id,mon1,mon2,polym,tg_celsius",
                "1,CCO,CCN,CCO,100",
                "2,CCO,CCC,CCO,125",
                "3,CCN,CCC,CCN,90",
                "4,CCO,CCC,not-a-smiles,80",
                "5,CCC,CCO,CCC,130",
            ]
        ),
        encoding="utf-8",
    )


def write_pi_iupac_csv(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "id,mon1,mon1_iupac,mon2,mon2_iupac,polym,tg_celsius",
                "1,CCO,ethanol,CCN,ethanamine,CCO,100",
                "2,CCO,ethanol,CCC,propane,CCO,125",
            ]
        ),
        encoding="utf-8",
    )


def build_reverse_design_app(tmp_path: Path, *, progress_interval_rows: int | None = None) -> FastAPI:
    csv_path = tmp_path / "pi.csv"
    main_db_path = tmp_path / "polyprop.db"
    pi_db_path = tmp_path / "pi.db"
    write_pi_csv(csv_path)
    import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=pi_db_path, progress_interval=0)

    settings = Settings(
        sqlite_db_path=str(main_db_path),
        csv_source_path=str(tmp_path / "source.csv"),
        pi_reverse_db_path=str(pi_db_path),
        pi_reverse_csv_path=str(csv_path),
        pi_reverse_backend="sqlite",
        pi_reverse_progress_interval_rows=progress_interval_rows,
        allowed_origins="http://localhost:5173",
        model_enabled=False,
    )
    return create_app(settings)


def test_reverse_design_service_sorts_sample_by_tg_difference(tmp_path: Path) -> None:
    csv_path = tmp_path / "pi.csv"
    db_path = tmp_path / "pi.db"
    write_pi_csv(csv_path)
    import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=db_path, progress_interval=0)

    with sqlite_connection(db_path) as connection:
        result = search_reverse_design_by_tg(
            connection,
            "CCO",
            120,
            similarity_threshold=0.0,
            candidate_sample_size=10,
            top_k=3,
            random_seed=1,
        )

    assert result.candidate_pool_size == 4
    assert result.sampled_candidate_count == 4
    assert result.scanned_rows == 4
    assert [candidate.pi_id for candidate in result.results] == [2, 5, 1]
    assert result.results[0].tg_difference == 5


def test_reverse_design_service_returns_top_matches_by_tg_difference(tmp_path: Path) -> None:
    csv_path = tmp_path / "pi.csv"
    db_path = tmp_path / "pi.db"
    write_pi_csv(csv_path)
    import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=db_path, progress_interval=0)

    with sqlite_connection(db_path) as connection:
        result = search_reverse_design_by_tg(
            connection,
            "CCO",
            120,
            similarity_threshold=0.0,
            candidate_sample_size=2,
            top_k=2,
        )

    assert result.candidate_pool_size == 4
    assert result.sampled_candidate_count == 4
    assert [candidate.pi_id for candidate in result.results] == [2, 5]


def test_reverse_design_service_reports_sqlite_scan_progress(tmp_path: Path) -> None:
    csv_path = tmp_path / "pi.csv"
    db_path = tmp_path / "pi.db"
    write_pi_csv(csv_path)
    import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=db_path, progress_interval=0)
    progress_events: list[dict[str, object]] = []

    with sqlite_connection(db_path) as connection:
        result = search_reverse_design_by_tg(
            connection,
            "CCO",
            120,
            similarity_threshold=0.0,
            top_k=2,
            progress_callback=lambda **progress: progress_events.append(progress),
            progress_interval_rows=2,
        )

    assert result.scanned_rows == 4
    assert [event["scanned_rows"] for event in progress_events] == [2, 4]
    assert progress_events[-1]["matched_count"] == 4
    assert progress_events[-1]["best_similarity_score"] is not None


def test_reverse_design_sqlite_route_forwards_scan_progress(tmp_path: Path) -> None:
    app = build_reverse_design_app(tmp_path, progress_interval_rows=2)
    progress_events: list[dict[str, object]] = []

    response = _search_by_tg_response(
        ReverseDesignTgRequest(smiles="CCO", target_tg=120, similarity_threshold=0.0),
        app,
        progress_callback=lambda **progress: progress_events.append(progress),
    )

    assert response.total == 4
    assert [event["scanned_rows"] for event in progress_events[:2]] == [2, 4]
    assert progress_events[-1]["scanned_rows"] == 4


class FakePostgresCursor:
    def __init__(self, connection: "FakePostgresConnection") -> None:
        self.connection = connection
        self.executions: list[tuple[str, tuple[object, ...]]] = []
        self.active_rows: list[dict[str, object]] = []

    def __enter__(self) -> "FakePostgresCursor":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executions.append((sql, params))
        self.connection.executions.append((sql, params))

        if "ORDER BY t.tg_celsius ASC" in sql:
            self.active_rows = self.connection.up_batches.pop(0) if self.connection.up_batches else []
        elif "ORDER BY t.tg_celsius DESC" in sql:
            self.active_rows = self.connection.down_batches.pop(0) if self.connection.down_batches else []
        elif "WHERE p.id = ANY" in sql:
            ids = list(params[0])
            self.active_rows = [self.connection.detail_rows[pi_id] for pi_id in ids]
        else:
            self.active_rows = []

    def fetchmany(self, chunk_size: int) -> list[dict[str, object]]:
        rows = self.active_rows[:chunk_size]
        self.active_rows = self.active_rows[chunk_size:]
        return rows


class FakePostgresConnection:
    def __init__(
        self,
        *,
        up_batches: list[list[dict[str, object]]],
        down_batches: list[list[dict[str, object]]],
        detail_rows: dict[int, dict[str, object]],
    ) -> None:
        self.up_batches = up_batches
        self.down_batches = down_batches
        self.detail_rows = detail_rows
        self.executions: list[tuple[str, tuple[object, ...]]] = []

    def cursor(self) -> FakePostgresCursor:
        return FakePostgresCursor(self)


def _postgres_light_row(
    pi_id: int,
    polym: str,
    tg_celsius: float,
) -> dict[str, object]:
    return {
        "pi_id": pi_id,
        "tg_celsius": tg_celsius,
        "morgan_fp": fingerprint_to_bytes(generate(polym)),
    }


def _postgres_detail_row(
    pi_id: int,
    polym: str,
    tg_celsius: float,
    *,
    mon1: str = "CCO",
    mon2: str = "CCN",
    mon1_iupac_name: str | None = "ethanol",
    mon2_iupac_name: str | None = "ethanamine",
) -> dict[str, object]:
    return {
        "pi_id": pi_id,
        "mon1": mon1,
        "mon2": mon2,
        "polym": polym,
        "canonical_polym": None,
        "tg_celsius": tg_celsius,
        "morgan_fp": fingerprint_to_bytes(generate(polym)),
        "mon1_iupac_name": mon1_iupac_name,
        "mon2_iupac_name": mon2_iupac_name,
    }


def test_postgres_reverse_design_scans_by_tg_distance_and_fetches_details_after_match() -> None:
    connection = FakePostgresConnection(
        up_batches=[
            [
                _postgres_light_row(1, "CCO", 102.0),
                _postgres_light_row(2, "CCC", 103.0),
            ],
        ],
        down_batches=[
            [
                _postgres_light_row(3, "CCO", 99.0),
            ],
        ],
        detail_rows={
            1: _postgres_detail_row(1, "CCO", 102.0),
            3: _postgres_detail_row(3, "CCO", 99.0, mon2="CCC", mon2_iupac_name="propane"),
        },
    )

    result = search_reverse_design_by_tg_postgres(
        connection,
        "CCO",
        100.0,
        similarity_threshold=1.0,
        result_limit=2,
        batch_size=2,
        max_scan_rows=10,
    )

    assert result.candidate_pool_size == 2
    assert result.sampled_candidate_count == 2
    assert [candidate.pi_id for candidate in result.results] == [3, 1]
    assert result.results[0].tg_difference == 1.0
    assert result.results[0].monomer_b_iupac == "propane"
    assert "pi_monomer_iupac" not in connection.executions[0][0]
    assert "ORDER BY t.tg_celsius ASC" in connection.executions[0][0]
    assert "ORDER BY t.tg_celsius DESC" in connection.executions[1][0]
    assert "pi_monomer_iupac" in connection.executions[-1][0]


def test_tanimoto_fingerprint_bytes_matches_rdkit_tanimoto() -> None:
    first = generate("CCO")
    second = generate("CCN")

    assert tanimoto_fingerprint_bytes(fingerprint_to_bytes(first), fingerprint_to_bytes(second)) == pytest.approx(
        tanimoto(first, second)
    )


def test_postgres_reverse_design_reports_exhaustion_after_scanning_all_rows() -> None:
    connection = FakePostgresConnection(
        up_batches=[
            [
                _postgres_light_row(1, "CCO", 102.0),
            ],
        ],
        down_batches=[
            [
                _postgres_light_row(2, "CCC", 99.0),
            ],
        ],
        detail_rows={
            1: _postgres_detail_row(1, "CCO", 102.0),
        },
    )
    progress_events: list[object] = []

    result = search_reverse_design_by_tg_postgres(
        connection,
        "CCO",
        100.0,
        similarity_threshold=1.0,
        result_limit=2,
        batch_size=2,
        max_scan_rows=None,
        timeout_seconds=0,
        progress_callback=progress_events.append,
        progress_interval_rows=1,
    )

    assert result.exhausted is True
    assert result.stopped_by_limit is False
    assert result.scanned_rows == 2
    assert result.candidate_pool_size == 1
    assert [candidate.pi_id for candidate in result.results] == [1]
    assert progress_events[-1].exhausted is True


def test_reverse_design_job_manager_reports_found_enough_status() -> None:
    manager = ReverseDesignJobManager(max_workers=1)

    def run_search(progress_callback, is_cancelled):
        progress_callback(scanned_rows=2, matched_count=1, current_tg_radius=1.5, best_similarity_score=0.9)
        return ReverseDesignTgResponse(
            target_tg=120,
            query_time_ms=12.0,
            candidate_pool_size=200,
            sampled_candidate_count=200,
            total=200,
            results=[],
        )

    job = manager.create_job(
        ReverseDesignTgRequest(smiles="CCO", target_tg=120, similarity_threshold=0.7),
        run_search,
    )
    manager.wait_for_job(job.job_id, timeout=2)

    status = manager.get_job(job.job_id)
    assert status.status == "found_enough"
    assert status.scanned_rows == 2
    assert status.matched_count == 200
    assert status.result is not None
    assert status.result.total == 200
    manager.shutdown(wait=True)


def test_reverse_design_job_api_returns_terminal_status(tmp_path: Path) -> None:
    app = build_reverse_design_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reverse-design/tg/jobs",
            json={
                "smiles": "CCO",
                "target_tg": 120,
                "similarity_threshold": 0.0,
            },
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]

        status_payload = None
        for _ in range(20):
            status_response = client.get(f"/api/v1/reverse-design/tg/jobs/{job_id}")
            assert status_response.status_code == 200
            status_payload = status_response.json()
            if status_payload["status"] in {"found_enough", "exhausted", "failed", "cancelled"}:
                break
            sleep(0.05)

        assert status_payload is not None
        assert status_payload["status"] == "exhausted"
        assert status_payload["scanned_rows"] == 4
        assert status_payload["result"]["total"] == 4


def test_reverse_design_request_ignores_removed_client_limits() -> None:
    request = ReverseDesignTgRequest(
        smiles="CCO",
        target_tg=120,
        similarity_threshold=0.7,
        candidate_sample_size=10,
        top_k=50,
        random_seed=1,
    )

    assert request.smiles == "CCO"
    assert request.target_tg == 120
    assert not hasattr(request, "candidate_sample_size")


def test_reverse_design_request_rejects_unknown_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ReverseDesignTgRequest(
            smiles="CCO",
            target_tg=120,
            similarity_threshold=0.7,
            unexpected_limit=10,
        )


@pytest.mark.asyncio
async def test_reverse_design_api_returns_candidates(tmp_path: Path) -> None:
    app = build_reverse_design_app(tmp_path)
    request = make_request(app)

    response = await search_by_tg(
        ReverseDesignTgRequest(
            smiles="CCO",
            target_tg=120,
            similarity_threshold=0.0,
        ),
        request,
    )

    assert response.target_tg == 120
    assert response.candidate_pool_size == 4
    assert response.sampled_candidate_count == 4
    assert response.total == 4
    assert response.results[0].rank == 1
    assert response.results[0].pi_id == 2
    assert response.results[0].structure_svg is not None
    assert response.results[0].monomer_a_structure_svg is not None
    assert response.results[0].monomer_b_structure_svg is not None
    assert "<svg" in response.results[0].monomer_a_structure_svg
    assert "<svg" in response.results[0].monomer_b_structure_svg


@pytest.mark.asyncio
async def test_reverse_design_api_returns_candidate_iupac_names(tmp_path: Path) -> None:
    csv_path = tmp_path / "pi.csv"
    main_db_path = tmp_path / "polyprop.db"
    pi_db_path = tmp_path / "pi.db"
    write_pi_iupac_csv(csv_path)
    import_pi_candidates_to_sqlite(csv_path=csv_path, db_path=pi_db_path, progress_interval=0)

    settings = Settings(
        sqlite_db_path=str(main_db_path),
        csv_source_path=str(tmp_path / "source.csv"),
        pi_reverse_db_path=str(pi_db_path),
        pi_reverse_csv_path=str(csv_path),
        pi_reverse_backend="sqlite",
        allowed_origins="http://localhost:5173",
        model_enabled=False,
    )
    app = create_app(settings)
    request = make_request(app)

    response = await search_by_tg(
        ReverseDesignTgRequest(
            smiles="CCO",
            target_tg=120,
            similarity_threshold=0.0,
        ),
        request,
    )

    assert response.results[0].monomer_a_iupac == "ethanol"
    assert response.results[0].monomer_b_iupac == "propane"


@pytest.mark.asyncio
async def test_reverse_design_api_rejects_invalid_smiles(tmp_path: Path) -> None:
    app = build_reverse_design_app(tmp_path)
    request = make_request(app)

    with pytest.raises(HTTPException) as exc_info:
        await search_by_tg(
            ReverseDesignTgRequest(
                smiles="not-a-smiles",
                target_tg=120,
            ),
            request,
        )

    assert exc_info.value.status_code == 422
    assert "invalid smiles" in exc_info.value.detail


@pytest.mark.asyncio
async def test_reverse_design_api_reports_uninitialized_database(tmp_path: Path) -> None:
    settings = Settings(
        sqlite_db_path=str(tmp_path / "polyprop.db"),
        csv_source_path=str(tmp_path / "source.csv"),
        pi_reverse_db_path=str(tmp_path / "empty_pi.db"),
        pi_reverse_backend="sqlite",
        allowed_origins="http://localhost:5173",
        model_enabled=False,
    )
    app = create_app(settings)
    request = make_request(app)

    with pytest.raises(HTTPException) as exc_info:
        await search_by_tg(
            ReverseDesignTgRequest(
                smiles="CCO",
                target_tg=120,
            ),
            request,
        )

    assert exc_info.value.status_code == 503
    assert "not initialized" in exc_info.value.detail
