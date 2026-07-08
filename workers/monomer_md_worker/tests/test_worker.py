from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from workers.monomer_md_worker.app import main as worker_main
from workers.monomer_md_worker.app.byteff2_formal_runner import ByteFF2FormalRunner
from workers.monomer_md_worker.app.config import WorkerSettings
from workers.monomer_md_worker.app.models import JobRequest
from workers.monomer_md_worker.app.repository import PostgresJobRepository


def _settings(
    tmp_path: Path,
    *,
    mode: str = "dry-run",
    app_postgres_dsn: str | None = None,
    max_active_jobs: int = 1,
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
        max_active_jobs=max_active_jobs,
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


def test_real_health_reports_missing_gmx_after_import_probe(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn="postgresql://db/app")
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    calls = []
    envs = []

    def fake_run(args, **kwargs):
        calls.append(args)
        envs.append(kwargs.get("env", {}))
        if args[0] == settings.byteff2_python:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=args,
            returncode=127,
            stdout="",
            stderr="gmx: command not found\n",
        )

    monkeypatch.setattr(worker_main.subprocess, "run", fake_run)

    response = worker_main._build_health_response()

    assert response.status == "degraded"
    assert response.runtime_ready is False
    assert "gmx" in (response.runtime_error or "")
    assert response.protocols["Density"]["runtime_ready"] is False
    assert "gmx" in response.protocols["Density"]["runtime_error"]
    assert any(call == ["gmx", "--version"] for call in calls)
    assert all(env.get("CUDA_VISIBLE_DEVICES") == "2" for env in envs if env)


def test_real_health_reports_missing_configured_demo_entry(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn="postgresql://db/app")
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setenv("BYTEFF2_DENSITY_DEMO_ENTRY", "missing_demo.py")

    def fake_run(*args, **kwargs):
        raise AssertionError("runtime probes should not run when the configured demo entry is missing")

    monkeypatch.setattr(worker_main.subprocess, "run", fake_run)

    response = worker_main._build_health_response()

    assert response.status == "degraded"
    assert response.runtime_ready is False
    assert "BYTEFF2_DENSITY_DEMO_ENTRY" in (response.runtime_error or "")


def test_submit_rejects_unready_formal_protocol(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn="postgresql://db/app")
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "active_jobs", {})
    protocols = {
        protocol: {
            "protocol": protocol,
            "run_mode": "formal",
            "supported": True,
            "runtime_ready": True,
            "runtime_error": None,
        }
        for protocol in ("Density", "HVap", "Compressibility", "Dielectric", "Transport")
    }
    protocols["Transport"]["runtime_ready"] = False
    protocols["Transport"]["runtime_error"] = "velocityverletplugin is not importable"
    monkeypatch.setattr(worker_main, "_probe_real_runtime", lambda: (True, None, protocols))

    client = TestClient(worker_main.app)
    response = client.post(
        "/jobs",
        json={
            "job_id": "formal-transport-1",
            "smiles": "{}",
            "canonical_smiles": "{}",
            "steps": 15000000,
            "protocol": "Transport",
            "run_mode": "formal",
            "config_json": {"protocol": "Transport"},
        },
    )

    assert response.status_code == 503
    assert "velocityverletplugin" in response.json()["detail"]
    assert worker_main.active_jobs == {}


def test_submit_rejects_when_active_capacity_is_full(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, app_postgres_dsn=None, max_active_jobs=1)
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "active_jobs", {"busy-job": object()})

    client = TestClient(worker_main.app)
    response = client.post(
        "/jobs",
        json={"job_id": "job-1", "smiles": "CCO", "canonical_smiles": "CCO", "steps": 1000},
    )

    assert response.status_code == 429
    assert response.json()["detail"] == "monomer MD worker active job capacity is full"
    assert set(worker_main.active_jobs) == {"busy-job"}


def test_submit_rejects_when_formal_capacity_is_full(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn="postgresql://db/app", max_active_jobs=10)
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "active_jobs", {"formal-busy": object()})
    monkeypatch.setattr(worker_main, "active_formal_jobs", {"formal-busy"})
    protocols = {
        protocol: {
            "protocol": protocol,
            "run_mode": "formal",
            "supported": True,
            "runtime_ready": True,
            "runtime_error": None,
        }
        for protocol in ("Density", "HVap", "Compressibility", "Dielectric", "Transport")
    }
    monkeypatch.setattr(worker_main, "_probe_real_runtime", lambda: (True, None, protocols))

    client = TestClient(worker_main.app)
    response = client.post(
        "/jobs",
        json={
            "job_id": "formal-density-2",
            "smiles": "{}",
            "canonical_smiles": "{}",
            "steps": 1500000,
            "protocol": "Density",
            "run_mode": "formal",
            "config_json": {"protocol": "Density"},
        },
    )

    assert response.status_code == 429
    assert "formal ByteFF2" in response.json()["detail"]
    assert worker_main.active_formal_jobs == {"formal-busy"}


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


def test_formal_runner_writes_config_and_parses_density_result(tmp_path: Path):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn=None)
    settings.byteff2_root.mkdir()
    object.__setattr__(settings, "byteff2_python", sys.executable)
    run_md = settings.byteff2_root / "example" / "4_MD_simulations" / "run_md.py"
    run_md.parent.mkdir(parents=True)
    run_md.write_text(
        "\n".join(
            [
                "import argparse, json, pathlib",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--config')",
                "args = parser.parse_args()",
                "config = json.loads(pathlib.Path(args.config).read_text())",
                "out = pathlib.Path(config['output_dir'])",
                "out.mkdir(parents=True, exist_ok=True)",
                "(out / 'density_results.json').write_text(json.dumps({'density': 1.02, 'density_std': 0.01}))",
            ]
        ),
        encoding="utf-8",
    )
    runner = ByteFF2FormalRunner(settings)
    request = JobRequest(
        job_id="formal-density-1",
        smiles='{"DMC": "COC(=O)OC"}',
        canonical_smiles='{"DMC": "COC(=O)OC"}',
        steps=1500000,
        protocol="Density",
        run_mode="formal",
        config_json={
            "protocol": "Density",
            "params_dir": "unsafe-params",
            "output_dir": "unsafe-output",
            "working_dir": "unsafe-working",
            "temperature": 298,
            "natoms": 10000,
            "components": {"DMC": 1},
            "smiles": {"DMC": "COC(=O)OC"},
        },
    )

    result = runner.run(request, tmp_path / "job-root")

    config = (tmp_path / "job-root" / "config.json").read_text(encoding="utf-8")
    assert str(tmp_path / "job-root" / "outputs") in config
    assert result.completed_steps == 1500000
    assert result.result["summary"]["density"] == 1.02
    assert result.result["metrics"]["density_std"] == 0.01
    assert any(
        item["path"] == "outputs/density_results.json"
        for item in result.result["artifact_manifest"]["files"]
    )


def test_delete_job_artifacts_removes_output_directory(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, app_postgres_dsn=None)
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "runner", worker_main.MonomerMdRunner(settings))
    monkeypatch.setattr(worker_main, "active_jobs", {})
    output_dir = settings.job_root / "job-1"
    output_dir.mkdir(parents=True)
    (output_dir / "density_demo_results.json").write_text("{}", encoding="utf-8")

    client = TestClient(worker_main.app)
    response = client.delete("/jobs/job-1/artifacts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] is True
    assert not output_dir.exists()


def test_repository_update_query_guards_terminal_statuses():
    source_names = PostgresJobRepository._build_update.__code__.co_consts

    assert any(
        isinstance(value, str)
        and "NOT IN ('completed', 'failed', 'cancelled')" in value
        for value in source_names
    )
