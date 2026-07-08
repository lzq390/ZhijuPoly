from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from workers.polytao_worker.app import main as worker_main
from workers.polytao_worker.app.config import WorkerSettings
from workers.polytao_worker.app.models import JobRequest
from workers.polytao_worker.app.polytao import PolytaoGenerationResult, RuntimeProbe
from workers.polytao_worker.app.repository import PostgresJobRepository


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


def _settings(tmp_path: Path, *, mode: str = "real", app_postgres_dsn: str = "postgresql://db/app") -> WorkerSettings:
    return WorkerSettings(
        host="127.0.0.1",
        port=8020,
        mode=mode,
        worker_id="polytao-test-worker",
        worker_version="test",
        app_postgres_dsn=app_postgres_dsn,
        db_configured=bool(app_postgres_dsn),
        model_dir=tmp_path / "polytao",
        model_id="hkqiu/PolymerGenerationPretrainedModel",
        model_revision=None,
        device="cpu",
        max_active_jobs=1,
        max_concurrent_jobs=1,
        health_probe_timeout_seconds=1,
        default_candidate_count=10,
        default_temperature=1.0,
        default_top_k=100,
        default_top_p=0.999,
        default_max_length=300,
    )


class FakeRepository:
    def __init__(self, *, health: tuple[bool, str | None] = (True, None), update_counts: list[int] | None = None) -> None:
        self.health = health
        self.update_counts = update_counts or []
        self.updates: list[dict[str, Any]] = []

    def health_check(self) -> tuple[bool, str | None]:
        return self.health

    def update_status(self, job_id: str, status: str, **kwargs: Any) -> int:
        self.updates.append({"job_id": job_id, "status": status, **kwargs})
        if self.update_counts:
            return self.update_counts.pop(0)
        return 1


class FakeRuntime:
    def __init__(self, *, probe: RuntimeProbe | None = None) -> None:
        self._probe = probe or RuntimeProbe(model_files_ready=True, runtime_ready=True)

    def probe(self) -> RuntimeProbe:
        return self._probe

    def generate(self, **kwargs: Any) -> PolytaoGenerationResult:
        result = {
            "prompt": kwargs["prompt"],
            "query_time_ms": 1.5,
            "requested_count": kwargs["candidate_count"],
            "returned_count": 1,
            "attempts": 1,
            "filter_counter": {},
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


def _install_fakes(
    monkeypatch,
    tmp_path: Path,
    *,
    settings: WorkerSettings | None = None,
    repository: FakeRepository | None = None,
    runtime: FakeRuntime | None = None,
) -> FakeRepository:
    active_settings = settings or _settings(tmp_path)
    active_repository = repository or FakeRepository()
    monkeypatch.setattr(worker_main, "settings", active_settings)
    monkeypatch.setattr(worker_main, "repository", active_repository)
    monkeypatch.setattr(worker_main, "runtime", runtime or FakeRuntime())
    monkeypatch.setattr(worker_main, "semaphore", asyncio.Semaphore(active_settings.max_concurrent_jobs))
    monkeypatch.setattr(worker_main, "active_jobs", {})
    monkeypatch.setattr(worker_main, "active_jobs_lock", asyncio.Lock())
    return active_repository


def _job_payload() -> dict[str, Any]:
    return {
        "job_id": "job-1",
        "descriptors": DEFAULT_DESCRIPTORS,
        "prompt": "264,19,0,4,1,0,1,0,0,0,4,0,6,5,1",
        "candidate_count": 10,
        "temperature": 1.0,
        "top_k": 100,
        "top_p": 0.999,
        "max_length": 300,
    }


def test_health_reports_degraded_when_database_table_is_missing(tmp_path: Path, monkeypatch) -> None:
    _install_fakes(monkeypatch, tmp_path, repository=FakeRepository(health=(False, "generation.polytao_jobs table is missing")))
    client = TestClient(worker_main.app)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["db_configured"] is True
    assert payload["db_ready"] is False
    assert payload["db_error"] == "generation.polytao_jobs table is missing"


def test_health_reports_ok_when_database_and_runtime_are_ready(tmp_path: Path, monkeypatch) -> None:
    _install_fakes(monkeypatch, tmp_path)
    client = TestClient(worker_main.app)

    response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["db_ready"] is True
    assert payload["runtime_ready"] is True


def test_submit_rejects_degraded_database_without_status_update(tmp_path: Path, monkeypatch) -> None:
    repository = _install_fakes(
        monkeypatch,
        tmp_path,
        repository=FakeRepository(health=(False, "generation.polytao_jobs table is missing")),
    )
    client = TestClient(worker_main.app)

    response = client.post("/jobs", json=_job_payload())

    assert response.status_code == 503
    assert "database is not ready" in response.json()["detail"]
    assert repository.updates == []


def test_submit_fails_closed_when_initial_status_update_misses_row(tmp_path: Path, monkeypatch) -> None:
    repository = _install_fakes(monkeypatch, tmp_path, repository=FakeRepository(update_counts=[0]))
    client = TestClient(worker_main.app)

    response = client.post("/jobs", json=_job_payload())

    assert response.status_code == 503
    assert response.json()["detail"] == "PolyTAO job row is not available for status updates"
    assert repository.updates[0]["status"] == "submitted"


def test_run_job_records_fake_generation_result(tmp_path: Path, monkeypatch) -> None:
    repository = _install_fakes(monkeypatch, tmp_path)
    request = JobRequest(**_job_payload())

    asyncio.run(worker_main._run_job(request))

    statuses = [update["status"] for update in repository.updates]
    assert statuses == ["running", "completed"]
    completed = repository.updates[-1]
    assert completed["returned_count"] == 1
    assert completed["progress_percent"] == 100
    assert completed["result"]["results"][0]["generated_smiles"] == "*CC*"


def test_repository_update_keeps_terminal_job_state_guard() -> None:
    source = inspect.getsource(PostgresJobRepository._build_update)

    assert "status NOT IN ('completed', 'failed', 'cancelled')" in source
