from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.postgres_database import postgres_connection
from app.routers.polytao import router as polytao_router
from app.services.polytao import normalize_polytao_candidates, polytao_prompt_from_descriptors
from app.services.polytao_repository import (
    create_polytao_job_postgres,
    mark_polytao_job_completed_postgres,
    mark_polytao_job_failed_postgres,
    mark_polytao_job_running_postgres,
    mark_polytao_job_submitted_postgres,
    mark_stale_polytao_jobs_failed_postgres,
)
from app.services.polytao_runtime import PolytaoGenerationResult, RuntimeProbe


DEFAULT_DESCRIPTORS = {
    "MolWt": 264,
    "HeavyAtomCount": 19,
    "NHOHCount": 0,
    "NOCount": 4,
    "NumAliphaticCarbocycles": 1,
    "NumAliphaticHeterocycles": 0,
    "NumAliphaticRings": 1,
    "NumAromaticCarbocycles": 0,
    "NumAromaticHeterocycles": 0,
    "NumAromaticRings": 0,
    "NumHAcceptors": 4,
    "NumHDonors": 0,
    "NumHeteroatoms": 6,
    "NumRotatableBonds": 5,
    "RingCount": 1,
}


class FakePolytaoRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def probe(self) -> RuntimeProbe:
        return RuntimeProbe(model_files_ready=True, runtime_ready=True)

    def generate(self, **kwargs: Any) -> PolytaoGenerationResult:
        self.calls.append(kwargs)
        result = {
            "prompt": kwargs["prompt"],
            "query_time_ms": 1.5,
            "requested_count": kwargs["candidate_count"],
            "returned_count": 1,
            "attempts": 1,
            "filter_counter": {"duplicate": 1},
            "results": [
                {
                    "rank": 1,
                    "generated_smiles": "*CC*",
                    "raw_smiles": "[*]CC[*]",
                    "valid_smiles": True,
                    "sa_score": None,
                    "warnings": [],
                }
            ],
        }
        return PolytaoGenerationResult(result=result, query_time_ms=1.5, returned_count=1)


class DegradedPolytaoRuntime:
    def probe(self) -> RuntimeProbe:
        return RuntimeProbe(
            model_files_ready=False,
            runtime_ready=False,
            runtime_error="missing PolyTAO model files: config.json",
        )

    def generate(self, **kwargs: Any) -> PolytaoGenerationResult:
        raise AssertionError("degraded PolyTAO runtime must not receive jobs")


class FailingPolytaoRuntime:
    def probe(self) -> RuntimeProbe:
        return RuntimeProbe(model_files_ready=True, runtime_ready=True)

    def generate(self, **kwargs: Any) -> PolytaoGenerationResult:
        raise RuntimeError("backend runtime failed")


class ImmediatePolytaoJobManager:
    def __init__(self, postgres_dsn: str) -> None:
        self._postgres_dsn = postgres_dsn

    def submit_job(self, job_id: str, runner) -> None:
        with postgres_connection(self._postgres_dsn) as connection:
            mark_polytao_job_submitted_postgres(connection, job_id=job_id)
            mark_polytao_job_running_postgres(connection, job_id)
        try:
            result = runner()
        except Exception as exc:
            with postgres_connection(self._postgres_dsn) as connection:
                mark_polytao_job_failed_postgres(connection, job_id, str(exc))
            return
        with postgres_connection(self._postgres_dsn) as connection:
            mark_polytao_job_completed_postgres(
                connection,
                job_id=job_id,
                result=result.result,
                returned_count=result.returned_count,
            )

    def shutdown(self, *, wait: bool = False) -> None:
        return None


def _create_app(
    postgres_dsn: str,
    *,
    runtime: object | None = None,
    polytao_enabled: bool = True,
    polytao_max_active_jobs: int = 1,
    polytao_rate_limit_per_ip_per_minute: int = 5,
    polytao_rate_limit_window_seconds: int = 60,
):
    settings = Settings(
        app_postgres_dsn=postgres_dsn,
        pi_postgres_dsn=postgres_dsn,
        lab_data_postgres_dsn=postgres_dsn,
        csv_source_path="database/data1.csv",
        experimental_process_csv_path="database/missing_process.csv",
        experimental_property_csv_path="database/missing_property.csv",
        allowed_origins="http://localhost:5173",
        structured_data_backend="postgres",
        pi_reverse_backend="postgres",
        model_enabled=False,
        polytao_enabled=polytao_enabled,
        polytao_max_active_jobs=polytao_max_active_jobs,
        polytao_rate_limit_per_ip_per_minute=polytao_rate_limit_per_ip_per_minute,
        polytao_rate_limit_window_seconds=polytao_rate_limit_window_seconds,
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.polytao_runtime = runtime if runtime is not None else FakePolytaoRuntime()
    app.state.polytao_job_manager = ImmediatePolytaoJobManager(postgres_dsn)
    app.include_router(polytao_router)
    return app


def _polytao_job_count(postgres_dsn: str) -> int:
    with postgres_connection(postgres_dsn) as connection:
        row = connection.execute("SELECT count(*) AS count FROM generation.polytao_jobs").fetchone()
        return int(row["count"])


def _request_payload(**overrides):
    payload = {
        "descriptors": DEFAULT_DESCRIPTORS,
        "input_smiles": None,
        "candidate_count": 10,
        "temperature": 1.0,
        "top_k": 100,
        "top_p": 0.999,
        "max_length": 300,
    }
    payload.update(overrides)
    return payload


def _seed_polytao_job(postgres_dsn: str, job_id: str = "job-1") -> None:
    with postgres_connection(postgres_dsn) as connection:
        create_polytao_job_postgres(
            connection,
            job_id=job_id,
            input_smiles=None,
            canonical_smiles=None,
            descriptor_prompt=polytao_prompt_from_descriptors(DEFAULT_DESCRIPTORS),
            descriptors=DEFAULT_DESCRIPTORS,
            request_data=_request_payload(),
            requested_count=10,
        )


def test_polytao_descriptor_endpoint_calculates_rdkit_prompt(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, polytao_enabled=False))

    response = client.post("/api/v1/conditional-generation/polytao/descriptors", json={"smiles": "CCO"})

    assert response.status_code == 200
    data = response.json()
    assert data["canonical_smiles"] == "CCO"
    assert len(data["descriptors"]) == 15
    assert data["descriptors"][0]["name"] == "MolWt"
    assert data["descriptors"][1] == {"name": "HeavyAtomCount", "value": 3.0}
    assert len(data["prompt"].split(",")) == 15


def test_polytao_status_reports_disabled_backend_runtime(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, polytao_enabled=False))

    response = client.get("/api/v1/conditional-generation/polytao/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["available"] is False
    assert data["worker_base_url_configured"] is False
    assert data["message"] == "PolyTAO backend runtime is disabled"


def test_polytao_status_checks_backend_runtime(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn))

    response = client.get("/api/v1/conditional-generation/polytao/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is True
    assert data["worker_status"] is None
    assert data["worker_mode"] is None
    assert data["runtime_ready"] is True
    assert data["message"] == "PolyTAO backend runtime is ready"


def test_polytao_status_reports_degraded_backend_runtime(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, runtime=DegradedPolytaoRuntime()))

    response = client.get("/api/v1/conditional-generation/polytao/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is False
    assert data["runtime_ready"] is False
    assert data["runtime_error"] == "missing PolyTAO model files: config.json"
    assert data["message"] == "PolyTAO backend runtime is not ready: missing PolyTAO model files: config.json"


def test_polytao_status_reports_database_not_ready(monkeypatch, postgres_dsn: str):
    monkeypatch.setattr(
        "app.routers.polytao._polytao_db_health",
        lambda settings: (True, False, "generation.polytao_jobs table is missing", None),
    )
    client = TestClient(_create_app(postgres_dsn))

    response = client.get("/api/v1/conditional-generation/polytao/status")

    assert response.status_code == 200
    data = response.json()
    assert data["available"] is False
    assert data["db_ready"] is False
    assert data["db_error"] == "generation.polytao_jobs table is missing"
    assert data["message"] == "PolyTAO backend database is not ready: generation.polytao_jobs table is missing"


def test_polytao_job_requires_enabled_backend_runtime(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, polytao_enabled=False))

    response = client.post("/api/v1/conditional-generation/polytao/jobs", json=_request_payload())

    assert response.status_code == 503
    assert response.json()["detail"] == "PolyTAO backend runtime is disabled"
    assert _polytao_job_count(postgres_dsn) == 0


def test_polytao_job_rejects_degraded_runtime_without_creating_row(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, runtime=DegradedPolytaoRuntime()))

    response = client.post("/api/v1/conditional-generation/polytao/jobs", json=_request_payload())

    assert response.status_code == 503
    assert "backend runtime is not ready" in response.json()["detail"]
    assert _polytao_job_count(postgres_dsn) == 0


def test_polytao_job_submit_and_status_roundtrip(postgres_dsn: str):
    fake_runtime = FakePolytaoRuntime()
    client = TestClient(_create_app(postgres_dsn, runtime=fake_runtime))

    create_response = client.post("/api/v1/conditional-generation/polytao/jobs", json=_request_payload(input_smiles="CCO"))

    assert create_response.status_code == 202
    job_id = create_response.json()["job_id"]
    assert fake_runtime.calls[0]["prompt"] == "264,19,0,4,1,0,1,0,0,0,4,0,6,5,1"

    status_response = client.get(f"/api/v1/conditional-generation/polytao/jobs/{job_id}")
    assert status_response.status_code == 200
    completed = status_response.json()
    assert completed["status"] == "completed"
    assert completed["engine"] == "polytao-backend"
    assert completed["progress_message"] == "PolyTAO generation completed in the backend runtime."
    assert completed["result"]["returned_count"] == 1
    assert "<svg" in completed["result"]["results"][0]["structure_svg"]


def test_polytao_job_submit_rate_limit_is_per_ip(postgres_dsn: str):
    app = _create_app(
        postgres_dsn,
        polytao_max_active_jobs=10,
        polytao_rate_limit_per_ip_per_minute=1,
        polytao_rate_limit_window_seconds=60,
    )
    client = TestClient(app)
    headers = {"x-forwarded-for": "203.0.113.9"}

    first_response = client.post(
        "/api/v1/conditional-generation/polytao/jobs",
        json=_request_payload(),
        headers=headers,
    )
    second_response = client.post(
        "/api/v1/conditional-generation/polytao/jobs",
        json=_request_payload(),
        headers=headers,
    )

    assert first_response.status_code == 202
    assert second_response.status_code == 429
    assert second_response.json()["detail"] == "PolyTAO submit rate limit exceeded; please wait before submitting another job"


def test_polytao_job_capacity_counts_active_postgres_jobs(postgres_dsn: str):
    _seed_polytao_job(postgres_dsn, job_id="active-job")
    with postgres_connection(postgres_dsn) as connection:
        mark_polytao_job_running_postgres(connection, "active-job")
    client = TestClient(_create_app(postgres_dsn, polytao_max_active_jobs=1))

    response = client.post("/api/v1/conditional-generation/polytao/jobs", json=_request_payload())

    assert response.status_code == 429
    assert response.json()["detail"] == "PolyTAO job capacity is full; please wait for the current job to finish"


def test_polytao_backend_runtime_failure_marks_job_failed(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, runtime=FailingPolytaoRuntime()))

    response = client.post("/api/v1/conditional-generation/polytao/jobs", json=_request_payload())

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    status_response = client.get(f"/api/v1/conditional-generation/polytao/jobs/{job_id}")
    assert status_response.status_code == 200
    data = status_response.json()
    assert data["status"] == "failed"
    assert data["error_message"] == "backend runtime failed"


def test_polytao_generation_request_rejects_missing_descriptor(postgres_dsn: str):
    payload = _request_payload(descriptors={k: v for k, v in DEFAULT_DESCRIPTORS.items() if k != "MolWt"})
    client = TestClient(_create_app(postgres_dsn))

    response = client.post("/api/v1/conditional-generation/polytao/jobs", json=payload)

    assert response.status_code == 422


def test_polytao_terminal_state_updates_do_not_regress(postgres_dsn: str):
    _seed_polytao_job(postgres_dsn, job_id="terminal-job")
    result = {
        "prompt": polytao_prompt_from_descriptors(DEFAULT_DESCRIPTORS),
        "query_time_ms": 1.0,
        "requested_count": 10,
        "returned_count": 0,
        "attempts": 1,
        "filter_counter": {},
        "results": [],
    }
    with postgres_connection(postgres_dsn) as connection:
        mark_polytao_job_completed_postgres(connection, job_id="terminal-job", result=result, returned_count=0)
        mark_polytao_job_running_postgres(connection, "terminal-job")
        mark_polytao_job_failed_postgres(connection, "terminal-job", "late failure")
        row = connection.execute(
            "SELECT status, error_message, result_data FROM generation.polytao_jobs WHERE job_id = %s",
            ("terminal-job",),
        ).fetchone()

    assert row["status"] == "completed"
    assert row["error_message"] is None
    result_data = json.loads(row["result_data"]) if isinstance(row["result_data"], str) else row["result_data"]
    assert result_data["returned_count"] == 0


def test_polytao_startup_cleanup_marks_stale_active_jobs_failed(postgres_dsn: str):
    _seed_polytao_job(postgres_dsn, job_id="stale-job")
    with postgres_connection(postgres_dsn) as connection:
        mark_polytao_job_running_postgres(connection, "stale-job")
        mark_stale_polytao_jobs_failed_postgres(connection)
        row = connection.execute(
            "SELECT status, error_message FROM generation.polytao_jobs WHERE job_id = %s",
            ("stale-job",),
        ).fetchone()

    assert row["status"] == "failed"
    assert row["error_message"] == "PolyTAO backend restarted before this job finished."


def test_polytao_candidate_normalization_filters_invalid_duplicates_and_attachment_points():
    candidates, filters = normalize_polytao_candidates(
        ["", "not-a-smiles", "CCO", "[*]CC[*]", "*CC*", "*OCC*"],
        requested_count=10,
    )

    assert [candidate.generated_smiles for candidate in candidates] == ["*CC*", "*CCO*"]
    assert filters["empty_raw_smiles"] == 1
    assert filters["rdkit_parse_failed"] == 1
    assert filters["star_count_lt_2"] == 1
    assert filters["duplicate"] == 1
