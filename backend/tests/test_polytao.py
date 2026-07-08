from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.postgres_database import postgres_connection
from app.routers.polytao import router as polytao_router
from app.services.polytao import normalize_polytao_candidates
from app.services.polytao_worker_client import PolytaoWorkerClient, PolytaoWorkerError


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


class FakeWorkerClient:
    def __init__(self) -> None:
        self.payloads = []

    def get_health(self):
        return {
            "status": "ok",
            "mode": "real",
            "db_configured": True,
            "runtime_ready": True,
            "active_jobs": 0,
            "model_id": "hkqiu/PolymerGenerationPretrainedModel",
            "worker_version": "test",
            "default_params": {
                "candidate_count": 10,
                "temperature": 1.0,
                "top_k": 100,
                "top_p": 0.999,
                "max_length": 300,
            },
        }

    def submit_job(self, payload):
        self.payloads.append(payload)
        return type(
            "Submission",
            (),
            {
                "worker_id": "polytao-test-worker",
                "worker_job_id": payload.job_id,
                "worker_version": "test",
            },
        )()


class FailingWorkerClient:
    def get_health(self):
        raise PolytaoWorkerError("PolyTAO worker is not reachable")

    def submit_job(self, payload):
        raise PolytaoWorkerError("PolyTAO worker is not reachable")


class DegradedWorkerClient:
    def get_health(self):
        return {
            "status": "ok",
            "mode": "real",
            "db_configured": True,
            "runtime_ready": False,
            "runtime_error": "missing PolyTAO model files: config.json",
            "active_jobs": 0,
        }

    def submit_job(self, payload):
        raise AssertionError("degraded workers must not receive submitted jobs")


class SubmitFailingWorkerClient:
    def get_health(self):
        return {
            "status": "ok",
            "mode": "real",
            "db_configured": True,
            "runtime_ready": True,
            "active_jobs": 0,
        }

    def submit_job(self, payload):
        raise PolytaoWorkerError("worker returned invalid JSON")


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b"{}",
        json_data=None,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.content = content
        self._json_data = json_data
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._json_data


def _create_app(
    postgres_dsn: str,
    *,
    worker_url: str = "http://polytao-worker:18020",
    polytao_submit_enabled: bool = True,
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
        polytao_worker_base_url=worker_url,
        polytao_submit_enabled=polytao_submit_enabled,
        polytao_max_active_jobs=polytao_max_active_jobs,
        polytao_rate_limit_per_ip_per_minute=polytao_rate_limit_per_ip_per_minute,
        polytao_rate_limit_window_seconds=polytao_rate_limit_window_seconds,
    )
    app = FastAPI()
    app.state.settings = settings
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


def test_polytao_descriptor_endpoint_calculates_rdkit_prompt(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, worker_url=""))

    response = client.post("/api/v1/conditional-generation/polytao/descriptors", json={"smiles": "CCO"})

    assert response.status_code == 200
    data = response.json()
    assert data["canonical_smiles"] == "CCO"
    assert len(data["descriptors"]) == 15
    assert data["descriptors"][0]["name"] == "MolWt"
    assert data["descriptors"][1] == {"name": "HeavyAtomCount", "value": 3.0}
    assert len(data["prompt"].split(",")) == 15


def test_polytao_status_reports_disabled_worker(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, worker_url=""))

    response = client.get("/api/v1/conditional-generation/polytao/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["available"] is False
    assert data["worker_base_url_configured"] is False


def test_polytao_status_checks_worker_health(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.polytao_worker_client = FakeWorkerClient()
    client = TestClient(app)

    response = client.get("/api/v1/conditional-generation/polytao/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is True
    assert data["worker_status"] == "ok"
    assert data["worker_mode"] == "real"
    assert data["runtime_ready"] is True


def test_polytao_status_reports_unreachable_worker(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.polytao_worker_client = FailingWorkerClient()
    client = TestClient(app)

    response = client.get("/api/v1/conditional-generation/polytao/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is False
    assert data["worker_status"] == "unreachable"
    assert data["message"] == "PolyTAO worker is not reachable"


def test_polytao_job_requires_configured_worker(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, worker_url=""))

    response = client.post("/api/v1/conditional-generation/polytao/jobs", json=_request_payload())

    assert response.status_code == 503
    assert response.json()["detail"] == "PolyTAO worker is not configured"
    assert _polytao_job_count(postgres_dsn) == 0


def test_polytao_job_rejects_degraded_worker_without_creating_row(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.polytao_worker_client = DegradedWorkerClient()
    client = TestClient(app)

    response = client.post("/api/v1/conditional-generation/polytao/jobs", json=_request_payload())

    assert response.status_code == 503
    assert "runtime is not ready" in response.json()["detail"]
    assert _polytao_job_count(postgres_dsn) == 0


def test_polytao_job_submit_and_status_roundtrip(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    fake_worker = FakeWorkerClient()
    app.state.polytao_worker_client = fake_worker
    client = TestClient(app)

    create_response = client.post("/api/v1/conditional-generation/polytao/jobs", json=_request_payload(input_smiles="CCO"))

    assert create_response.status_code == 202
    job_id = create_response.json()["job_id"]
    assert fake_worker.payloads[0].job_id == job_id
    assert fake_worker.payloads[0].prompt == "264,19,0,4,1,0,1,0,0,0,4,0,6,5,1"

    status_response = client.get(f"/api/v1/conditional-generation/polytao/jobs/{job_id}")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "submitted"

    result = {
        "prompt": fake_worker.payloads[0].prompt,
        "query_time_ms": 123.4,
        "requested_count": 10,
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
    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            """
            UPDATE generation.polytao_jobs
            SET status = 'completed',
                result_data = %s::jsonb,
                returned_count = 1,
                attempts = 1,
                progress_percent = 100,
                progress_stage = 'completed',
                progress_message = 'done',
                finished_at = now(),
                updated_at = now()
            WHERE job_id = %s
            """,
            (json.dumps(result), job_id),
        )

    completed_response = client.get(f"/api/v1/conditional-generation/polytao/jobs/{job_id}")
    assert completed_response.status_code == 200
    completed = completed_response.json()
    assert "<svg" in completed["result"]["results"][0]["structure_svg"]


def test_polytao_job_submit_rate_limit_is_per_ip(postgres_dsn: str):
    app = _create_app(
        postgres_dsn,
        polytao_max_active_jobs=10,
        polytao_rate_limit_per_ip_per_minute=1,
        polytao_rate_limit_window_seconds=60,
    )
    app.state.polytao_worker_client = FakeWorkerClient()
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


def test_polytao_worker_submission_failure_marks_job_failed(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.polytao_worker_client = SubmitFailingWorkerClient()
    client = TestClient(app)

    response = client.post("/api/v1/conditional-generation/polytao/jobs", json=_request_payload())

    assert response.status_code == 503
    with postgres_connection(postgres_dsn) as connection:
        row = connection.execute("SELECT status, error_message FROM generation.polytao_jobs").fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "worker returned invalid JSON"


def test_polytao_generation_request_rejects_missing_descriptor(postgres_dsn: str):
    payload = _request_payload(descriptors={k: v for k, v in DEFAULT_DESCRIPTORS.items() if k != "MolWt"})
    client = TestClient(_create_app(postgres_dsn))

    response = client.post("/api/v1/conditional-generation/polytao/jobs", json=payload)

    assert response.status_code == 422


def test_polytao_worker_client_rejects_submit_response_without_job_id(monkeypatch):
    def fake_post(*args, **kwargs):
        return FakeResponse(content=b"{}", json_data={"status": "submitted"})

    monkeypatch.setattr("app.services.polytao_worker_client.requests.Session.post", fake_post)
    client = PolytaoWorkerClient(base_url="http://worker.test", timeout_seconds=1)

    with pytest.raises(PolytaoWorkerError, match="without returning a job id"):
        client.submit_job(
            type(
                "Payload",
                (),
                {
                    "to_json": lambda self: {
                        "job_id": "job-1",
                        "descriptors": DEFAULT_DESCRIPTORS,
                    }
                },
            )()
        )


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
