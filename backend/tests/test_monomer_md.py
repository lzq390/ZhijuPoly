from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.postgres_database import postgres_connection
from app.routers.monomer_md import router as monomer_md_router
from app.services.monomer_md_repository import (
    create_monomer_md_job_postgres,
    mark_monomer_md_job_completed_postgres,
    mark_monomer_md_job_failed_postgres,
    mark_monomer_md_job_submitted_postgres,
)
from app.services.monomer_md_worker_client import (
    MonomerMdWorkerClient,
    MonomerMdWorkerError,
    MonomerMdWorkerSubmitPayload,
)


class FakeWorkerClient:
    def __init__(self) -> None:
        self.payloads = []

    def get_health(self):
        return {
            "status": "ok",
            "mode": "dry-run",
            "db_configured": True,
            "active_jobs": 0,
        }

    def submit_job(self, payload):
        self.payloads.append(payload)
        return type(
            "Submission",
            (),
            {
                "worker_id": "fake-worker",
                "worker_job_id": payload.job_id,
                "worker_version": "test",
            },
        )()


class FailingWorkerClient:
    def get_health(self):
        raise MonomerMdWorkerError("monomer MD worker is not reachable")

    def submit_job(self, payload):
        raise MonomerMdWorkerError("monomer MD worker is not reachable")


class DegradedWorkerClient:
    def get_health(self):
        return {
            "status": "ok",
            "mode": "real",
            "db_configured": False,
            "byteff2_root_exists": True,
            "runtime_ready": True,
            "active_jobs": 0,
        }

    def submit_job(self, payload):
        raise AssertionError("degraded workers must not receive submitted jobs")


class SubmitFailingWorkerClient:
    def get_health(self):
        return {
            "status": "ok",
            "mode": "dry-run",
            "db_configured": True,
            "active_jobs": 0,
        }

    def submit_job(self, payload):
        raise MonomerMdWorkerError("worker returned invalid JSON")


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        content: bytes = b"{}",
        json_data=None,
        json_error: Exception | None = None,
    ):
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
    worker_url: str = "http://monomer-md-worker:18082",
    monomer_md_submit_enabled: bool = True,
    monomer_md_rate_limit_per_ip_per_minute: int = 3,
    monomer_md_rate_limit_window_seconds: int = 60,
    monomer_md_max_active_jobs: int = 1,
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
        monomer_md_worker_base_url=worker_url,
        monomer_md_default_steps=1000,
        monomer_md_submit_enabled=monomer_md_submit_enabled,
        monomer_md_rate_limit_per_ip_per_minute=monomer_md_rate_limit_per_ip_per_minute,
        monomer_md_rate_limit_window_seconds=monomer_md_rate_limit_window_seconds,
        monomer_md_max_active_jobs=monomer_md_max_active_jobs,
    )
    app = FastAPI()
    app.state.settings = settings
    app.include_router(monomer_md_router)
    return app


def _monomer_md_job_count(postgres_dsn: str) -> int:
    with postgres_connection(postgres_dsn) as connection:
        row = connection.execute(
            "SELECT count(*) AS count FROM md.monomer_md_jobs"
        ).fetchone()
        return int(row["count"])


def test_monomer_md_status_reports_disabled_worker(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, worker_url=""))

    response = client.get("/api/v1/monomer-md/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["available"] is False
    assert data["default_steps"] == 1000


def test_monomer_md_status_checks_configured_worker_health(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = FakeWorkerClient()
    client = TestClient(app)

    response = client.get("/api/v1/monomer-md/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is True
    assert data["worker_status"] == "ok"
    assert data["worker_mode"] == "dry-run"
    assert data["db_configured"] is True


def test_monomer_md_status_reports_unreachable_worker(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = FailingWorkerClient()
    client = TestClient(app)

    response = client.get("/api/v1/monomer-md/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is False
    assert data["worker_status"] == "unreachable"
    assert data["message"] == "monomer MD worker is not reachable"


def test_monomer_md_status_reports_invalid_worker_url(postgres_dsn: str):
    app = _create_app(postgres_dsn, worker_url="not-a-url")
    client = TestClient(app)

    response = client.get("/api/v1/monomer-md/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is False
    assert data["worker_status"] == "unreachable"
    assert "MONOMER_MD_WORKER_BASE_URL" in data["message"]


def test_monomer_md_job_requires_configured_worker(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, worker_url=""))

    response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})

    assert response.status_code == 503
    assert response.json()["detail"] == "monomer MD worker is not configured"


def test_monomer_md_job_rejects_disabled_submit_without_creating_row(postgres_dsn: str):
    app = _create_app(postgres_dsn, monomer_md_submit_enabled=False)
    app.state.monomer_md_worker_client = FakeWorkerClient()
    client = TestClient(app)

    status_response = client.get("/api/v1/monomer-md/status")
    assert status_response.status_code == 200
    assert status_response.json()["enabled"] is False
    assert status_response.json()["available"] is False
    assert status_response.json()["message"] == "monomer MD submissions are disabled"

    response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})

    assert response.status_code == 503
    assert response.json()["detail"] == "monomer MD submissions are disabled"
    assert _monomer_md_job_count(postgres_dsn) == 0


def test_monomer_md_job_rejects_invalid_worker_url_without_creating_row(postgres_dsn: str):
    app = _create_app(postgres_dsn, worker_url="not-a-url")
    client = TestClient(app)

    response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})

    assert response.status_code == 503
    assert "MONOMER_MD_WORKER_BASE_URL" in response.json()["detail"]
    assert _monomer_md_job_count(postgres_dsn) == 0


def test_monomer_md_job_rejects_degraded_worker_without_creating_row(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = DegradedWorkerClient()
    client = TestClient(app)

    response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})

    assert response.status_code == 503
    assert response.json()["detail"] == "monomer MD worker database is not configured"
    assert _monomer_md_job_count(postgres_dsn) == 0


def test_monomer_md_job_rejects_non_json_health_without_creating_row(postgres_dsn: str, monkeypatch):
    app = _create_app(postgres_dsn, worker_url="http://worker.test")
    client = TestClient(app)

    def fake_get(*args, **kwargs):
        return FakeResponse(content=b"not json", json_error=ValueError("bad json"))

    monkeypatch.setattr("app.services.monomer_md_worker_client.requests.Session.get", fake_get)

    response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})

    assert response.status_code == 503
    assert "invalid JSON" in response.json()["detail"]
    assert _monomer_md_job_count(postgres_dsn) == 0


def test_monomer_md_job_rate_limits_by_forwarded_client_ip_without_creating_extra_row(postgres_dsn: str):
    app = _create_app(
        postgres_dsn,
        monomer_md_rate_limit_per_ip_per_minute=1,
        monomer_md_rate_limit_window_seconds=60,
        monomer_md_max_active_jobs=10,
    )
    fake_worker = FakeWorkerClient()
    app.state.monomer_md_worker_client = fake_worker
    client = TestClient(app)
    headers = {"X-Forwarded-For": "203.0.113.10, 127.0.0.1"}

    first_response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"}, headers=headers)
    second_response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"}, headers=headers)

    assert first_response.status_code == 202
    assert second_response.status_code == 429
    assert "rate limit" in second_response.json()["detail"]
    assert _monomer_md_job_count(postgres_dsn) == 1
    assert len(fake_worker.payloads) == 1


def test_monomer_md_job_rejects_when_active_capacity_is_full_without_creating_extra_row(postgres_dsn: str):
    app = _create_app(postgres_dsn, monomer_md_rate_limit_per_ip_per_minute=10, monomer_md_max_active_jobs=1)
    fake_worker = FakeWorkerClient()
    app.state.monomer_md_worker_client = fake_worker
    client = TestClient(app)

    first_response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})
    second_response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})

    assert first_response.status_code == 202
    assert second_response.status_code == 429
    assert "capacity is full" in second_response.json()["detail"]
    assert _monomer_md_job_count(postgres_dsn) == 1
    assert len(fake_worker.payloads) == 1


def test_monomer_md_job_rejects_polymer_attachment_smiles(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = FakeWorkerClient()
    client = TestClient(app)

    response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "*CC*"})

    assert response.status_code == 422
    assert "single-molecule SMILES" in response.json()["detail"]


def test_monomer_md_job_rejects_invalid_smiles(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = FakeWorkerClient()
    client = TestClient(app)

    response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "not-a-smiles"})

    assert response.status_code == 422
    assert "invalid smiles" in response.json()["detail"]


def test_monomer_md_job_submit_and_status_roundtrip(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    fake_worker = FakeWorkerClient()
    app.state.monomer_md_worker_client = fake_worker

    with TestClient(app) as client:
        create_response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "OCC"})
        assert create_response.status_code == 202
        job_id = create_response.json()["job_id"]
        assert create_response.json()["status"] == "submitted"

        assert len(fake_worker.payloads) == 1
        assert fake_worker.payloads[0].job_id == job_id
        assert fake_worker.payloads[0].canonical_smiles == "CCO"
        assert fake_worker.payloads[0].steps == 1000

        status_response = client.get(f"/api/v1/monomer-md/jobs/{job_id}")
        assert status_response.status_code == 200
        status_payload = status_response.json()
        assert status_payload["status"] == "submitted"
        assert status_payload["input_smiles"] == "OCC"
        assert status_payload["canonical_smiles"] == "CCO"
        assert status_payload["requested_steps"] == 1000
        assert status_payload["worker_id"] == "fake-worker"

        with postgres_connection(postgres_dsn) as connection:
            mark_monomer_md_job_completed_postgres(
                connection,
                job_id=job_id,
                completed_steps=1000,
                artifact_root="/runs/job",
                artifacts={"npt_state_csv": {"relative_path": "npt_state.csv"}},
                result_data={
                    "summary": {"final_density_g_cm3": 0.78, "note": "demo only"},
                    "density_series": {
                        "key": "density",
                        "label": "Density",
                        "unit": "g/cm3",
                        "points": [{"step": 10, "time_ps": 0.02, "value": 0.75}],
                    },
                    "warnings": ["not equilibrated"],
                },
            )

        completed_response = client.get(f"/api/v1/monomer-md/jobs/{job_id}")
        assert completed_response.status_code == 200
        completed_payload = completed_response.json()
        assert completed_payload["status"] == "completed"
        assert completed_payload["completed_steps"] == 1000
        assert completed_payload["progress_percent"] == 100
        assert completed_payload["result"]["summary"]["final_density_g_cm3"] == 0.78
        assert completed_payload["result"]["density_series"]["points"][0]["step"] == 10


def test_monomer_md_worker_identity_update_does_not_regress_running_status(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    fake_worker = FakeWorkerClient()
    app.state.monomer_md_worker_client = fake_worker

    with TestClient(app) as client:
        create_response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})
        assert create_response.status_code == 202
        job_id = create_response.json()["job_id"]

        with postgres_connection(postgres_dsn) as connection:
            connection.execute(
                """
                UPDATE md.monomer_md_jobs
                SET status = 'running', progress_stage = 'running', progress_message = 'Worker already started.', progress_percent = 5
                WHERE job_id = %s
                """,
                (job_id,),
            )
            mark_monomer_md_job_submitted_postgres(
                connection,
                job_id=job_id,
                worker_id="late-backend",
                worker_job_id=job_id,
                worker_version="late",
            )

        status_response = client.get(f"/api/v1/monomer-md/jobs/{job_id}")
        assert status_response.status_code == 200
        status_payload = status_response.json()
        assert status_payload["status"] == "running"
        assert status_payload["progress_stage"] == "running"
        assert status_payload["progress_message"] == "Worker already started."
        assert status_payload["worker_id"] == "late-backend"


def test_monomer_md_repository_completed_update_does_not_override_failed(postgres_dsn: str):
    job_id = "terminal-guard-job"
    with postgres_connection(postgres_dsn) as connection:
        create_monomer_md_job_postgres(
            connection,
            job_id=job_id,
            input_smiles="CCO",
            canonical_smiles="CCO",
            requested_steps=1000,
        )
        mark_monomer_md_job_failed_postgres(connection, job_id, "worker failed")
        mark_monomer_md_job_completed_postgres(
            connection,
            job_id=job_id,
            completed_steps=1000,
            artifact_root="/runs/job",
            artifacts={"npt_state_csv": {"path": "npt_state.csv"}},
            result_data={"summary": {"final_density_g_cm3": 0.8}},
        )
        row = connection.execute(
            "SELECT status, error_message, result_data FROM md.monomer_md_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()

    assert row["status"] == "failed"
    assert row["error_message"] == "worker failed"
    assert row["result_data"] is None


def test_monomer_md_repository_failed_update_does_not_override_failed(postgres_dsn: str):
    job_id = "failed-terminal-guard-job"
    with postgres_connection(postgres_dsn) as connection:
        create_monomer_md_job_postgres(
            connection,
            job_id=job_id,
            input_smiles="CCO",
            canonical_smiles="CCO",
            requested_steps=1000,
        )
        mark_monomer_md_job_failed_postgres(connection, job_id, "first failure")
        mark_monomer_md_job_failed_postgres(connection, job_id, "late failure")
        row = connection.execute(
            "SELECT status, error_message, progress_message FROM md.monomer_md_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()

    assert row["status"] == "failed"
    assert row["error_message"] == "first failure"
    assert row["progress_message"] == "first failure"


def test_monomer_md_job_returns_404_for_unknown_job(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    client = TestClient(app)

    response = client.get("/api/v1/monomer-md/jobs/missing")

    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


def test_monomer_md_worker_submission_failure_returns_503(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = SubmitFailingWorkerClient()

    with TestClient(app) as client:
        response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})

    assert response.status_code == 503
    assert response.json()["detail"] == "worker returned invalid JSON"
    with postgres_connection(postgres_dsn) as connection:
        row = connection.execute(
            "SELECT status, error_message FROM md.monomer_md_jobs"
        ).fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "worker returned invalid JSON"


def test_monomer_md_worker_client_rejects_non_json_submit_response(monkeypatch):
    client = MonomerMdWorkerClient(base_url="http://worker.test", timeout_seconds=1)

    def fake_post(*args, **kwargs):
        return FakeResponse(
            status_code=202,
            content=b"accepted",
            json_error=ValueError("bad json"),
        )

    monkeypatch.setattr("app.services.monomer_md_worker_client.requests.Session.post", fake_post)

    payload = MonomerMdWorkerSubmitPayload(
        job_id="job-1",
        smiles="CCO",
        canonical_smiles="CCO",
        steps=1000,
    )
    try:
        client.submit_job(payload)
    except MonomerMdWorkerError as exc:
        assert "invalid JSON" in str(exc)
    else:
        raise AssertionError("non-JSON submit responses must fail")


def test_monomer_md_worker_client_uses_configured_health_timeout(monkeypatch):
    client = MonomerMdWorkerClient(base_url="http://worker.test", timeout_seconds=21)
    observed = {}

    def fake_get(*args, **kwargs):
        observed["timeout"] = kwargs.get("timeout")
        return FakeResponse(
            status_code=200,
            content=b'{"status":"ok"}',
            json_data={"status": "ok"},
        )

    monkeypatch.setattr("app.services.monomer_md_worker_client.requests.Session.get", fake_get)

    client.get_health()

    assert observed["timeout"] == 21


def test_monomer_md_worker_client_uses_unix_socket_connection(monkeypatch):
    observed = {}

    class FakeRawResponse:
        status = 200
        reason = "OK"

        def read(self):
            return b'{"status":"ok"}'

        def getheaders(self):
            return [("content-type", "application/json")]

    class FakeUnixConnection:
        def __init__(self, socket_path, timeout):
            observed["socket_path"] = socket_path
            observed["timeout"] = timeout

        def request(self, method, target, body=None, headers=None):
            observed["method"] = method
            observed["target"] = target
            observed["body"] = body
            observed["headers"] = headers

        def getresponse(self):
            return FakeRawResponse()

        def close(self):
            observed["closed"] = True

    monkeypatch.setattr(
        "app.services.monomer_md_worker_client._UnixSocketHTTPConnection",
        FakeUnixConnection,
    )

    client = MonomerMdWorkerClient(
        base_url="http+unix://%2Ftmp%2Fworker.sock",
        timeout_seconds=7,
    )
    health = client.get_health()

    assert health == {"status": "ok"}
    assert observed["socket_path"] == "/tmp/worker.sock"
    assert observed["timeout"] == 7
    assert observed["method"] == "GET"
    assert observed["target"] == "/health"
    assert observed["headers"]["Host"] == "monomer-md-worker"
    assert observed["closed"] is True


def test_monomer_md_worker_client_rejects_submit_response_without_job_id(monkeypatch):
    client = MonomerMdWorkerClient(base_url="http://worker.test", timeout_seconds=1)

    def fake_post(*args, **kwargs):
        return FakeResponse(status_code=202, content=b"{}", json_data={"status": "submitted"})

    monkeypatch.setattr("app.services.monomer_md_worker_client.requests.Session.post", fake_post)

    payload = MonomerMdWorkerSubmitPayload(
        job_id="job-1",
        smiles="CCO",
        canonical_smiles="CCO",
        steps=1000,
    )
    try:
        client.submit_job(payload)
    except MonomerMdWorkerError as exc:
        assert "without returning a job id" in str(exc)
    else:
        raise AssertionError("submit responses without a job id must fail")
