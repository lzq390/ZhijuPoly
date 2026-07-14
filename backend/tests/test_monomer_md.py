from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import monomer_md as monomer_md_routes
from app.config import Settings
from app.postgres_database import postgres_connection
from app.routers.monomer_md import router as monomer_md_router
from app.services.monomer_md_repository import (
    count_active_monomer_md_jobs_postgres,
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

FORMAL_PROTOCOLS = ("Density", "HVap", "Compressibility", "Dielectric", "Transport")


def _ready_protocols() -> dict[str, dict[str, object]]:
    return {
        protocol: {
            "protocol": protocol,
            "run_mode": "formal",
            "supported": True,
            "runtime_ready": True,
            "runtime_error": None,
        }
        for protocol in FORMAL_PROTOCOLS
    }


class FakeWorkerClient:
    def __init__(self, *, mode: str = "dry-run", protocols: dict[str, dict[str, object]] | None = None) -> None:
        self.payloads = []
        self.deleted_jobs = []
        self.mode = mode
        self.protocols = protocols if protocols is not None else _ready_protocols()

    def get_health(self):
        health = {
            "status": "ok",
            "mode": self.mode,
            "db_configured": True,
            "runtime_ready": True,
            "active_jobs": 0,
            "protocols": self.protocols,
        }
        if self.mode == "real":
            health["byteff2_root_exists"] = True
        return health

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

    def delete_artifacts(self, job_id: str):
        self.deleted_jobs.append(job_id)
        return {
            "job_id": job_id,
            "deleted": True,
            "artifact_root": f"/runs/{job_id}",
            "message": "artifacts deleted",
        }


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


class DegradedFormalReadyWorkerClient:
    def get_health(self):
        return {
            "status": "degraded",
            "mode": "real",
            "db_configured": False,
            "byteff2_root_exists": True,
            "runtime_ready": True,
            "active_jobs": 0,
            "protocols": _ready_protocols(),
        }

    def submit_job(self, payload):
        raise AssertionError("degraded workers must not receive submitted jobs")


class DrainingWorkerClient:
    def get_health(self):
        return {
            "status": "ok",
            "mode": "real",
            "db_configured": True,
            "byteff2_root_exists": True,
            "runtime_ready": True,
            "active_jobs": 1,
            "max_active_jobs": 1,
            "accepting_jobs": False,
            "draining": True,
            "protocols": _ready_protocols(),
        }

    def submit_job(self, payload):
        raise AssertionError("draining workers must not receive submitted jobs")


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
        monomer_md_default_steps=300,
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


def _density_formal_config() -> dict[str, object]:
    return {
        "protocol": "Density",
        "params_dir": "/tmp/user-supplied-params",
        "output_dir": "/tmp/user-supplied-output",
        "working_dir": "/tmp/user-supplied-working",
        "temperature": 298,
        "natoms": 10000,
        "components": {
            "DMC": 249,
            "EC": 170,
            "LI": 34,
            "PF6": 34,
        },
        "smiles": {
            "DMC": "COC(=O)OC",
            "EC": "O=C1OCCO1",
            "LI": "[Li+]",
            "PF6": "F[P-](F)(F)(F)(F)F",
        },
    }


def _dielectric_formal_config() -> dict[str, object]:
    return {
        "protocol": "Dielectric",
        "params_dir": "dielectric_params",
        "output_dir": "dielectric_results",
        "working_dir": "dielectric_working_dir",
        "temperature": 298,
        "natoms": 5000,
        "npt_steps": 2000000,
        "nvt_steps": 8000000,
        "dipole_interval": 1000,
        "components": {"DMC": 1},
        "smiles": {"DMC": "COC(=O)OC"},
    }


def test_monomer_md_status_reports_disabled_worker(postgres_dsn: str):
    client = TestClient(_create_app(postgres_dsn, worker_url=""))

    response = client.get("/api/v1/monomer-md/status")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["available"] is False
    assert data["default_steps"] == 300


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


def test_monomer_md_status_offloads_worker_and_database_io(
    postgres_dsn: str,
    monkeypatch,
):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = FakeWorkerClient()
    offloaded_functions = []

    async def fake_run_in_threadpool(function, *args, **kwargs):
        offloaded_functions.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(monomer_md_routes, "run_in_threadpool", fake_run_in_threadpool)

    response = TestClient(app).get("/api/v1/monomer-md/status")

    assert response.status_code == 200
    assert [function.__name__ for function in offloaded_functions] == [
        "get_health",
        "_database_active_job_count",
    ]


def test_monomer_md_job_lifecycle_offloads_worker_and_database_io(
    postgres_dsn: str,
    monkeypatch,
):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = FakeWorkerClient()
    offloaded_functions = []

    async def fake_run_in_threadpool(function, *args, **kwargs):
        offloaded_functions.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(monomer_md_routes, "run_in_threadpool", fake_run_in_threadpool)

    with TestClient(app) as client:
        created = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})
        assert created.status_code == 202
        loaded = client.get(f"/api/v1/monomer-md/jobs/{created.json()['job_id']}")

    assert loaded.status_code == 200
    assert [function.__name__ for function in offloaded_functions] == [
        "_job_request_details",
        "get_health",
        "_create_pending_job_with_capacity_guard",
        "submit_job",
        "_mark_job_submitted_and_get",
        "_get_job",
    ]


def test_monomer_md_protocols_returns_catalog_with_readiness(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = FakeWorkerClient(mode="real")
    client = TestClient(app)

    response = client.get("/api/v1/monomer-md/protocols")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is True
    protocols = {item["protocol"]: item for item in data["protocols"]}
    assert set(protocols) == set(FORMAL_PROTOCOLS)
    assert protocols["Density"]["runtime_ready"] is True
    assert protocols["Density"]["default_config"]["protocol"] == "Density"
    assert protocols["Transport"]["required_result_file"] == "results.json"


def test_monomer_md_protocols_reports_unavailable_when_worker_degraded(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = DegradedFormalReadyWorkerClient()
    client = TestClient(app)

    response = client.get("/api/v1/monomer-md/protocols")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is True
    assert data["available"] is False
    assert "degraded" in data["message"]


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
    assert second_response.json()["detail"] == "monomer MD job capacity is full; please wait for the active job to finish"
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
        assert fake_worker.payloads[0].steps == 300

        status_response = client.get(f"/api/v1/monomer-md/jobs/{job_id}")
        assert status_response.status_code == 200
        status_payload = status_response.json()
        assert status_payload["status"] == "submitted"
        assert status_payload["input_smiles"] == "OCC"
        assert status_payload["canonical_smiles"] == "CCO"
        assert status_payload["requested_steps"] == 300
        assert status_payload["worker_id"] == "fake-worker"

        with postgres_connection(postgres_dsn) as connection:
            mark_monomer_md_job_completed_postgres(
                connection,
                job_id=job_id,
                completed_steps=300,
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
        assert completed_payload["completed_steps"] == 300
        assert completed_payload["progress_percent"] == 100
        assert completed_payload["result"]["summary"]["final_density_g_cm3"] == 0.78
        assert completed_payload["result"]["density_series"]["points"][0]["step"] == 10


def test_monomer_md_formal_density_submit_and_status_roundtrip(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    fake_worker = FakeWorkerClient(mode="real")
    app.state.monomer_md_worker_client = fake_worker

    with TestClient(app) as client:
        create_response = client.post(
            "/api/v1/monomer-md/jobs",
            json={
                "protocol": "Density",
                "run_mode": "formal",
                "config_json": _density_formal_config(),
            },
        )
        assert create_response.status_code == 202
        job_id = create_response.json()["job_id"]

        assert len(fake_worker.payloads) == 1
        payload = fake_worker.payloads[0]
        assert payload.job_id == job_id
        assert payload.protocol == "Density"
        assert payload.run_mode == "formal"
        assert payload.steps == 1500000
        assert payload.config_json["params_dir"] == "managed_params"

        status_response = client.get(f"/api/v1/monomer-md/jobs/{job_id}")
        assert status_response.status_code == 200
        status_payload = status_response.json()
        assert status_payload["protocol"] == "Density"
        assert status_payload["run_mode"] == "formal"
        assert status_payload["requested_steps"] == 1500000
        assert status_payload["config_json"]["output_dir"] == "managed_output"
        assert status_payload["components"]["smiles"]["DMC"] == "COC(=O)OC"
        assert status_payload["engine"] == "byteff2-formal-worker"

        with postgres_connection(postgres_dsn) as connection:
            mark_monomer_md_job_completed_postgres(
                connection,
                job_id=job_id,
                completed_steps=1500000,
                artifact_root="/runs/formal-density",
                artifacts={"density_results_json": {"path": "outputs/density_results.json"}},
                artifact_manifest={"files": [{"path": "outputs/density_results.json"}]},
                result_summary={"density": 1.21, "density_std": 0.02},
                byteff2_git_sha="abc1234",
                gpu_device="2",
                result_data={
                    "protocol": "Density",
                    "run_mode": "formal",
                    "summary": {"density": 1.21, "density_std": 0.02},
                    "artifacts": {"outputs/density_results.json": {"path": "outputs/density_results.json"}},
                    "metrics": {"density": 1.21, "density_std": 0.02},
                },
            )

        completed_response = client.get(f"/api/v1/monomer-md/jobs/{job_id}")
        assert completed_response.status_code == 200
        completed_payload = completed_response.json()
        assert completed_payload["status"] == "completed"
        assert completed_payload["result_summary"]["density"] == 1.21
        assert completed_payload["artifact_manifest"]["files"][0]["path"] == "outputs/density_results.json"
        assert completed_payload["byteff2_git_sha"] == "abc1234"
        assert completed_payload["gpu_device"] == "2"
        assert completed_payload["result"]["metrics"]["density"] == 1.21


def test_monomer_md_formal_job_rejects_invalid_dielectric_step_config(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = FakeWorkerClient(mode="real")
    client = TestClient(app)
    config = _dielectric_formal_config()
    config["npt_steps"] = "bad"

    response = client.post(
        "/api/v1/monomer-md/jobs",
        json={"protocol": "Dielectric", "run_mode": "formal", "config_json": config},
    )

    assert response.status_code == 422
    assert "npt_steps" in response.json()["detail"]
    assert _monomer_md_job_count(postgres_dsn) == 0


def test_monomer_md_formal_job_capacity_is_always_one(postgres_dsn: str):
    app = _create_app(
        postgres_dsn,
        monomer_md_rate_limit_per_ip_per_minute=10,
        monomer_md_max_active_jobs=10,
    )
    fake_worker = FakeWorkerClient(mode="real")
    app.state.monomer_md_worker_client = fake_worker
    client = TestClient(app)

    first_response = client.post(
        "/api/v1/monomer-md/jobs",
        json={"protocol": "Density", "run_mode": "formal", "config_json": _density_formal_config()},
    )
    second_response = client.post(
        "/api/v1/monomer-md/jobs",
        json={"protocol": "Density", "run_mode": "formal", "config_json": _density_formal_config()},
    )

    assert first_response.status_code == 202
    assert second_response.status_code == 429
    assert "formal ByteFF2" in second_response.json()["detail"]
    assert _monomer_md_job_count(postgres_dsn) == 1
    assert len(fake_worker.payloads) == 1


def test_monomer_md_formal_job_rejects_unready_protocol_without_creating_row(postgres_dsn: str):
    protocols = _ready_protocols()
    protocols["Transport"] = {
        "protocol": "Transport",
        "run_mode": "formal",
        "supported": True,
        "runtime_ready": False,
        "runtime_error": "velocityverletplugin is not importable",
    }
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = FakeWorkerClient(mode="real", protocols=protocols)
    client = TestClient(app)

    config = _density_formal_config()
    config["protocol"] = "Transport"
    response = client.post(
        "/api/v1/monomer-md/jobs",
        json={"protocol": "Transport", "run_mode": "formal", "config_json": config},
    )

    assert response.status_code == 503
    assert "velocityverletplugin" in response.json()["detail"]
    assert _monomer_md_job_count(postgres_dsn) == 0


def test_monomer_md_artifact_delete_marks_job_and_preserves_audit(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    fake_worker = FakeWorkerClient()
    app.state.monomer_md_worker_client = fake_worker

    with TestClient(app) as client:
        create_response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})
        assert create_response.status_code == 202
        job_id = create_response.json()["job_id"]
        with postgres_connection(postgres_dsn) as connection:
            mark_monomer_md_job_completed_postgres(
                connection,
                job_id=job_id,
                completed_steps=300,
                artifact_root=f"/runs/{job_id}",
                artifacts={"npt_state_csv": {"path": "npt_state.csv"}},
                artifact_manifest={"files": [{"path": "npt_state.csv"}]},
                result_summary={"final_density_g_cm3": 0.8},
                result_data={"summary": {"final_density_g_cm3": 0.8}, "artifacts": {}},
            )

        delete_response = client.delete(f"/api/v1/monomer-md/jobs/{job_id}/artifacts")

    assert delete_response.status_code == 200
    payload = delete_response.json()
    assert fake_worker.deleted_jobs == [job_id]
    assert payload["status"] == "completed"
    assert payload["artifact_deleted_at"] is not None
    assert payload["artifact_delete_message"] == "artifacts deleted"
    assert payload["artifact_manifest"]["deleted"] is True
    assert payload["artifact_root"] == f"/runs/{job_id}"


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
            requested_steps=300,
        )
        mark_monomer_md_job_failed_postgres(connection, job_id, "worker failed")
        mark_monomer_md_job_completed_postgres(
            connection,
            job_id=job_id,
            completed_steps=300,
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
            requested_steps=300,
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
        steps=300,
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


def test_monomer_md_status_reports_database_capacity(postgres_dsn: str):
    app = _create_app(postgres_dsn, monomer_md_max_active_jobs=1)
    app.state.monomer_md_worker_client = FakeWorkerClient()
    with postgres_connection(postgres_dsn) as connection:
        create_monomer_md_job_postgres(
            connection,
            job_id="busy-job",
            input_smiles="CCO",
            canonical_smiles="CCO",
            requested_steps=300,
        )

    response = TestClient(app).get("/api/v1/monomer-md/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["can_submit"] is False
    assert payload["busy"] is True
    assert payload["active_jobs"] == 0
    assert payload["database_active_jobs"] == 1
    assert payload["max_active_jobs"] == 1
    assert payload["oldest_active_heartbeat_age_seconds"] is not None
    assert payload["oldest_active_heartbeat_age_seconds"] >= 0


def test_monomer_md_status_fails_closed_when_capacity_database_is_unavailable(
    postgres_dsn: str,
    monkeypatch,
):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = FakeWorkerClient()

    def unavailable_connection(_dsn):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("app.routers.monomer_md.postgres_connection", unavailable_connection)

    response = TestClient(app).get("/api/v1/monomer-md/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["can_submit"] is False
    assert payload["message"] == "monomer MD database capacity check failed"
    assert payload["database_active_jobs"] is None


def test_monomer_md_draining_worker_rejects_without_creating_row(postgres_dsn: str):
    app = _create_app(postgres_dsn)
    app.state.monomer_md_worker_client = DrainingWorkerClient()
    client = TestClient(app)

    status_response = client.get("/api/v1/monomer-md/status")
    create_response = client.post("/api/v1/monomer-md/jobs", json={"smiles": "CCO"})

    assert status_response.status_code == 200
    assert status_response.json()["available"] is True
    assert status_response.json()["draining"] is True
    assert status_response.json()["can_submit"] is False
    assert create_response.status_code == 503
    assert create_response.json()["detail"] == "monomer MD worker is draining for deployment"
    assert _monomer_md_job_count(postgres_dsn) == 0


def test_expired_unclaimed_pending_job_is_failed_before_capacity_count(postgres_dsn: str):
    app = _create_app(
        postgres_dsn,
        monomer_md_rate_limit_per_ip_per_minute=10,
        monomer_md_max_active_jobs=1,
    )
    fake_worker = FakeWorkerClient()
    app.state.monomer_md_worker_client = fake_worker
    with postgres_connection(postgres_dsn) as connection:
        create_monomer_md_job_postgres(
            connection,
            job_id="expired-pending",
            input_smiles="CCO",
            canonical_smiles="CCO",
            requested_steps=300,
        )
        connection.execute(
            "UPDATE md.monomer_md_jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s",
            ("expired-pending",),
        )

    response = TestClient(app).post("/api/v1/monomer-md/jobs", json={"smiles": "CCN"})

    assert response.status_code == 202
    with postgres_connection(postgres_dsn) as connection:
        expired = connection.execute(
            "SELECT status, error_category FROM md.monomer_md_jobs WHERE job_id = %s",
            ("expired-pending",),
        ).fetchone()
        assert count_active_monomer_md_jobs_postgres(connection) == 1
    assert expired["status"] == "failed"
    assert expired["error_category"] == "submit_lease_expired"


def test_status_reconciles_expired_unclaimed_pending_job(postgres_dsn: str):
    app = _create_app(postgres_dsn, monomer_md_max_active_jobs=1)
    app.state.monomer_md_worker_client = FakeWorkerClient()
    with postgres_connection(postgres_dsn) as connection:
        create_monomer_md_job_postgres(
            connection,
            job_id="expired-status-pending",
            input_smiles="CCO",
            canonical_smiles="CCO",
            requested_steps=300,
        )
        connection.execute(
            "UPDATE md.monomer_md_jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s",
            ("expired-status-pending",),
        )

    response = TestClient(app).get("/api/v1/monomer-md/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["database_active_jobs"] == 0
    assert payload["busy"] is False
    assert payload["can_submit"] is True
    with postgres_connection(postgres_dsn) as connection:
        row = connection.execute(
            "SELECT status, error_category FROM md.monomer_md_jobs WHERE job_id = %s",
            ("expired-status-pending",),
        ).fetchone()
    assert row["status"] == "failed"
    assert row["error_category"] == "submit_lease_expired"


def test_monomer_md_requested_steps_database_default_is_300(postgres_dsn: str):
    with postgres_connection(postgres_dsn) as connection:
        row = connection.execute(
            """
            SELECT column_default
            FROM information_schema.columns
            WHERE table_schema = 'md'
              AND table_name = 'monomer_md_jobs'
              AND column_name = 'requested_steps'
            """
        ).fetchone()

    assert row is not None
    assert str(row["column_default"]).strip("()") == "300"


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
        steps=300,
    )
    try:
        client.submit_job(payload)
    except MonomerMdWorkerError as exc:
        assert "without returning a job id" in str(exc)
    else:
        raise AssertionError("submit responses without a job id must fail")
