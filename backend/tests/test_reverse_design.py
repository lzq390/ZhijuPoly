from __future__ import annotations

from threading import get_ident
from time import sleep

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.config import Settings
from app.models import ReverseDesignTgRequest, ReverseDesignTgResponse
from app.routers import reverse_design as reverse_design_routes
from app.routers.reverse_design import _search_by_tg_response, search_by_tg
from app.services.fingerprint import fingerprint_to_bytes, generate, tanimoto
from app.services.postgres_reverse_design import (
    tanimoto_fingerprint_bytes,
    search_reverse_design_by_tg_postgres,
)
from app.services.reverse_design_jobs import ReverseDesignJobManager


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


def test_reverse_design_route_forwards_postgres_scan_progress(test_app: FastAPI) -> None:
    progress_events: list[dict[str, object]] = []

    response = _search_by_tg_response(
        ReverseDesignTgRequest(smiles="CCO", target_tg=215, similarity_threshold=0.0, candidate_size=1),
        test_app,
        progress_callback=lambda **progress: progress_events.append(progress),
    )

    assert response.total == 1
    assert response.results[0].pi_id == 7
    assert progress_events[-1]["scanned_rows"] >= 1


@pytest.mark.asyncio
async def test_reverse_design_tg_runs_synchronous_scan_off_event_loop(
    test_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread = get_ident()
    worker_threads: list[int] = []
    original_search = reverse_design_routes._search_by_tg_response

    def recording_search(*args, **kwargs):
        worker_threads.append(get_ident())
        return original_search(*args, **kwargs)

    monkeypatch.setattr(reverse_design_routes, "_search_by_tg_response", recording_search)

    response = await search_by_tg(
        ReverseDesignTgRequest(
            smiles="CCO",
            target_tg=215,
            similarity_threshold=0.0,
            candidate_size=1,
        ),
        make_request(test_app),
    )

    assert response.total == 1
    assert worker_threads and worker_threads[0] != event_loop_thread


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


def test_reverse_design_job_api_returns_terminal_status(test_app: FastAPI) -> None:
    with TestClient(test_app) as client:
        response = client.post(
            "/api/v1/reverse-design/tg/jobs",
            json={
                "smiles": "CCO",
                "target_tg": 215,
                "similarity_threshold": 0.0,
                "candidate_size": 1,
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
        assert status_payload["status"] == "found_enough"
        assert status_payload["scanned_rows"] >= 1
        assert status_payload["result"]["total"] == 1


def test_reverse_design_job_api_returns_503_when_executor_rejects(test_app: FastAPI) -> None:
    with TestClient(test_app) as client:
        manager = test_app.state.reverse_design_job_manager
        manager._executor.shutdown(wait=False)

        response = client.post(
            "/api/v1/reverse-design/tg/jobs",
            json={
                "smiles": "CCO",
                "target_tg": 215,
                "similarity_threshold": 0.0,
                "candidate_size": 1,
            },
        )

        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert response.json() == {
            "detail": "Reverse-design job service is temporarily unavailable."
        }
        assert manager.active_jobs == 0
        assert manager.active_executions == 0
        assert manager._jobs == {}


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


def test_reverse_design_request_accepts_candidate_size() -> None:
    default_request = ReverseDesignTgRequest(smiles="CCO", target_tg=120, similarity_threshold=0.7)
    explicit_request = ReverseDesignTgRequest(
        smiles="CCO",
        target_tg=120,
        similarity_threshold=0.7,
        candidate_size=25,
    )

    assert default_request.candidate_size == 200
    assert explicit_request.candidate_size == 25


def test_reverse_design_request_rejects_unknown_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ReverseDesignTgRequest(
            smiles="CCO",
            target_tg=120,
            similarity_threshold=0.7,
            unexpected_limit=10,
        )


@pytest.mark.asyncio
async def test_reverse_design_api_returns_candidates(test_app: FastAPI) -> None:
    request = make_request(test_app)

    response = await search_by_tg(
        ReverseDesignTgRequest(
            smiles="CCO",
            target_tg=215,
            similarity_threshold=0.0,
            candidate_size=1,
        ),
        request,
    )

    assert response.target_tg == 215
    assert response.candidate_pool_size == 1
    assert response.sampled_candidate_count >= 1
    assert response.total == 1
    assert response.results[0].rank == 1
    assert response.results[0].pi_id == 7
    assert response.results[0].structure_svg is not None
    assert response.results[0].monomer_a_structure_svg is not None
    assert response.results[0].monomer_b_structure_svg is not None
    assert "<svg" in response.results[0].monomer_a_structure_svg
    assert "<svg" in response.results[0].monomer_b_structure_svg


@pytest.mark.asyncio
async def test_reverse_design_api_returns_candidate_iupac_names(test_app: FastAPI) -> None:
    request = make_request(test_app)

    response = await search_by_tg(
        ReverseDesignTgRequest(
            smiles="CCO",
            target_tg=215,
            similarity_threshold=0.0,
            candidate_size=1,
        ),
        request,
    )

    assert response.results[0].monomer_a_iupac == "ethane-1,2-diamine"
    assert response.results[0].monomer_b_iupac == "carbon dioxide"


@pytest.mark.asyncio
async def test_reverse_design_api_rejects_invalid_smiles(test_app: FastAPI) -> None:
    request = make_request(test_app)

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


def test_settings_rejects_sqlite_reverse_design_backend() -> None:
    with pytest.raises(ValueError, match="PI_REVERSE_BACKEND must be 'postgres'"):
        Settings(
            pi_reverse_backend="sqlite",
            allowed_origins="http://localhost:5173",
            model_enabled=False,
        )
