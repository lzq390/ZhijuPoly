from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from workers.monomer_md_worker.app import main as worker_main
from workers.monomer_md_worker.app.config import WorkerSettings
from workers.monomer_md_worker.app.repository import PostgresJobRepository


def _settings(
    tmp_path: Path,
    *,
    mode: str = "dry-run",
    app_postgres_dsn: str | None = None,
) -> WorkerSettings:
    return WorkerSettings(
        mode=mode,  # type: ignore[arg-type]
        app_postgres_dsn=app_postgres_dsn,
        job_table="md.monomer_md_jobs",
        job_id_column="job_id",
        status_column="status",
        result_column="result_data",
        error_column="error_message",
        output_dir_column="artifact_root",
        artifacts_column="artifacts",
        completed_steps_column="completed_steps",
        progress_percent_column="progress_percent",
        progress_stage_column="progress_stage",
        progress_message_column="progress_message",
        worker_id_column="worker_id",
        worker_job_id_column="worker_job_id",
        worker_version_column="worker_version",
        started_at_column="started_at",
        finished_at_column="finished_at",
        updated_at_column="updated_at",
        byteff2_root=tmp_path / "byteff2",
        byteff2_python="python",
        byteff2_demo_command=None,
        job_root=tmp_path / "runs",
        default_steps=1000,
        max_steps=1000,
        report_interval=10,
        timeout_seconds=30,
        health_probe_timeout_seconds=5,
        max_concurrent_jobs=1,
        cuda_visible_devices="2",
        worker_id="test-worker",
        worker_version="test",
    )


def test_real_health_reports_runtime_probe_failure(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn="postgresql://db/app")
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'openmm'\n",
        )

    monkeypatch.setattr(worker_main.subprocess, "run", fake_run)

    response = worker_main._build_health_response()

    assert response.status == "degraded"
    assert response.mode == "real"
    assert response.db_configured is True
    assert response.byteff2_root_exists is True
    assert response.runtime_ready is False
    assert "openmm" in (response.runtime_error or "")


def test_submit_rejects_real_degraded_worker(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn=None)
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "active_jobs", {})
    monkeypatch.setattr(worker_main, "_probe_real_runtime", lambda: (True, None))

    client = TestClient(worker_main.app)
    response = client.post(
        "/jobs",
        json={"job_id": "job-1", "smiles": "CCO", "canonical_smiles": "CCO", "steps": 1000},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "monomer MD worker database is not configured"
    assert worker_main.active_jobs == {}


def test_submit_rejects_when_initial_status_update_fails(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, app_postgres_dsn="postgresql://db/app")
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "active_jobs", {})

    class MissingRowRepository:
        def update_status(self, *args, **kwargs):
            return 0

    monkeypatch.setattr(worker_main, "repository", MissingRowRepository())

    client = TestClient(worker_main.app)
    response = client.post(
        "/jobs",
        json={"job_id": "job-1", "smiles": "CCO", "canonical_smiles": "CCO", "steps": 1000},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "monomer MD job row is not available for status updates"
    assert worker_main.active_jobs == {}


def test_repository_update_query_guards_terminal_statuses():
    source_names = PostgresJobRepository._build_update.__code__.co_consts

    assert any(
        isinstance(value, str)
        and "NOT IN ('completed', 'failed', 'cancelled')" in value
        for value in source_names
    )
