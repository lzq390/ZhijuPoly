from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from workers.monomer_md_worker.app import byteff2_formal_runner as formal_runner_module
from workers.monomer_md_worker.app import main as worker_main
from workers.monomer_md_worker.app import process_control
from workers.monomer_md_worker.app.byteff2_env import REQUIRED_OPENMM_FILES
from workers.monomer_md_worker.app.byteff2_formal_runner import ByteFF2FormalRunner
from workers.monomer_md_worker.app.config import WorkerSettings
from workers.monomer_md_worker.app.models import JobRequest
from workers.monomer_md_worker.app.repository import JobUpdateResult, PostgresJobRepository
from workers.monomer_md_worker.app.runtime_health import (
    ProtocolRuntimeSnapshot,
    RuntimeSnapshot,
)


RELEASE_SHA = "a" * 40
RELEASE_TREE = "b" * 40
DIGEST = "sha256:" + "c" * 64


def _install_short_process_group_grace(monkeypatch) -> None:
    monkeypatch.setattr(
        process_control,
        "MAX_TERMINATION_GRACE_SECONDS",
        0.1,
    )
    monkeypatch.setattr(
        process_control,
        "PROCESS_GROUP_KILL_OBSERVE_SECONDS",
        0.2,
    )


def _production_runtime_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    module = source / "workers" / "monomer_md_worker" / "app" / "main.py"
    module.parent.mkdir(parents=True)
    module.write_text("# worker fixture\n", encoding="utf-8")
    venv = tmp_path / "runtime" / "worker-venvs" / "md-a" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(Path(sys.executable).resolve())
    return source, module, venv


def _fake_runtime_binding(venv: Path):
    checkout = SimpleNamespace(source_sha=RELEASE_SHA, source_tree=RELEASE_TREE)
    active = SimpleNamespace(
        slot="a",
        worker_lock_sha256=DIGEST,
        slot_record_sha256=DIGEST,
    )
    slot = SimpleNamespace(
        venv_prefix=str(venv),
        base_python_identity_sha256=DIGEST,
    )
    return checkout, SimpleNamespace(active=active, slot=slot), venv / "bin/python"


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
        default_steps=300,
        max_steps=300,
        report_interval=10,
        timeout_seconds=30,
        health_probe_timeout_seconds=5,
        max_concurrent_jobs=1,
        max_active_jobs=max_active_jobs,
        cuda_visible_devices="2",
        worker_id="test-worker",
        worker_version="test",
    )


def _runtime_snapshot(
    *,
    ready: bool = True,
    error: str | None = None,
    transport_ready: bool = True,
) -> RuntimeSnapshot:
    protocols = tuple(
        ProtocolRuntimeSnapshot(
            protocol,
            True,
            ready and (transport_ready if protocol == "Transport" else True),
            (
                "velocityverletplugin is not importable"
                if protocol == "Transport" and not transport_ready
                else error
            ),
        )
        for protocol in ("Density", "Transport", "HVap", "Dielectric", "Compressibility")
    )
    return RuntimeSnapshot(True, ready, error, protocols)


def test_health_exposes_source_and_venv_identity(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    identity = worker_main.RuntimeIdentity(
        source_sha=RELEASE_SHA,
        source_tree=RELEASE_TREE,
        source_root=str(tmp_path / "source"),
        venv_prefix=str(tmp_path / "venv"),
        venv_slot="a",
        worker_lock_sha256=DIGEST,
        slot_record_sha256=DIGEST,
        base_python_identity_sha256=DIGEST,
        python_executable=str(Path(sys.executable).resolve()),
    )
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "runtime_identity", identity)
    monkeypatch.setattr(worker_main, "active_jobs", {})
    monkeypatch.setattr(worker_main, "recovery_ready", True)

    response = asyncio.run(worker_main._build_health_response())

    assert response.source_sha == RELEASE_SHA
    assert response.source_tree == RELEASE_TREE
    assert response.source_root == identity.source_root
    assert response.venv_prefix == identity.venv_prefix
    assert response.venv_slot == "a"
    assert response.worker_lock_sha256 == DIGEST
    assert response.slot_record_sha256 == DIGEST
    assert response.base_python_identity_sha256 == DIGEST
    assert response.python_executable == identity.python_executable


def test_lifespan_starts_runtime_probe_and_initial_recovery_concurrently(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    snapshot = _runtime_snapshot()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "runtime_probe_initialized", False)
    monkeypatch.setattr(worker_main, "recovery_ready", False)
    monkeypatch.setattr(worker_main, "draining", False)
    started: set[str] = set()

    async def fake_probe(*_args, **_kwargs):
        started.add("probe")
        while "recovery" not in started:
            await asyncio.sleep(0)
        return snapshot

    async def fake_recovery() -> bool:
        started.add("recovery")
        while "probe" not in started:
            await asyncio.sleep(0)
        worker_main.recovery_ready = True
        return True

    monkeypatch.setattr(worker_main, "probe_runtime_snapshot", fake_probe)
    monkeypatch.setattr(worker_main, "_attempt_recovery", fake_recovery)

    async def scenario() -> None:
        async with worker_main.lifespan(worker_main.app):
            assert started == {"probe", "recovery"}
            assert worker_main.runtime_snapshot is snapshot
            assert worker_main.recovery_ready is True

    asyncio.run(scenario())


def test_probe_failure_does_not_disable_recovery_or_worker_lifecycle(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "runtime_probe_initialized", False)
    monkeypatch.setattr(worker_main, "recovery_ready", False)
    monkeypatch.setattr(worker_main, "draining", False)

    async def failed_probe(*_args, **_kwargs):
        raise RuntimeError("private runtime path")

    async def successful_recovery() -> bool:
        worker_main.recovery_ready = True
        return True

    monkeypatch.setattr(worker_main, "probe_runtime_snapshot", failed_probe)
    monkeypatch.setattr(worker_main, "_attempt_recovery", successful_recovery)

    async def scenario() -> None:
        async with worker_main.lifespan(worker_main.app):
            assert worker_main.recovery_ready is True
            assert worker_main.runtime_snapshot.runtime_ready is False
            assert worker_main.runtime_snapshot.runtime_error == (
                "monomer MD runtime startup probe failed"
            )
            assert "private runtime path" not in (
                worker_main.runtime_snapshot.runtime_error or ""
            )

    asyncio.run(scenario())


def test_runtime_snapshot_initializes_once_and_hot_readiness_never_reprobes(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    snapshot = _runtime_snapshot()
    calls = 0
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "runtime_probe_initialized", False)
    monkeypatch.setattr(worker_main, "recovery_ready", True)
    monkeypatch.setattr(worker_main, "active_jobs", {})
    monkeypatch.setattr(worker_main, "draining", False)

    async def fake_probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return snapshot

    monkeypatch.setattr(worker_main, "probe_runtime_snapshot", fake_probe)

    async def scenario() -> None:
        await worker_main._initialize_runtime_snapshot()
        await worker_main._initialize_runtime_snapshot()
        request = JobRequest(job_id="hot-read", smiles="CCO")
        for _ in range(30):
            response = await worker_main._build_health_response()
            assert worker_main._job_rejection_message(response, request) is None
            await worker_main.resume_worker()

    asyncio.run(scenario())
    assert calls == 1


def test_health_response_uses_one_immutable_runtime_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(
        tmp_path,
        mode="real",
        app_postgres_dsn="postgresql://db/app",
    )
    object.__setattr__(settings, "gpu_broker_enabled", True)
    initial = _runtime_snapshot()
    changed = _runtime_snapshot(
        ready=False,
        error="later global snapshot",
        transport_ready=False,
    )
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "runtime_snapshot", initial)
    monkeypatch.setattr(worker_main, "active_jobs", {})
    monkeypatch.setattr(worker_main, "recovery_ready", True)
    monkeypatch.setattr(worker_main, "draining", False)
    monkeypatch.setattr(
        worker_main,
        "runner",
        SimpleNamespace(gpu_admission_uncertain=False),
    )

    async def mutate_during_broker_read(_callback):
        worker_main.runtime_snapshot = changed
        return {"draining": False}

    monkeypatch.setattr(
        worker_main.asyncio,
        "to_thread",
        mutate_during_broker_read,
    )

    response = asyncio.run(worker_main._build_health_response())

    assert worker_main.runtime_snapshot is changed
    assert response.status == "ok"
    assert response.byteff2_root_exists is True
    assert response.runtime_ready is True
    assert response.runtime_error is None
    assert response.protocols == initial.protocols_dict()


def test_production_runtime_identity_uses_live_checkout_and_active_slot(
    tmp_path: Path, monkeypatch
):
    source, module, venv = _production_runtime_paths(tmp_path)
    monkeypatch.setattr(
        worker_main,
        "verify_runtime_binding",
        lambda **_kwargs: _fake_runtime_binding(venv),
    )

    identity = worker_main._load_runtime_identity(
        module_path=module,
        python_prefix=venv,
        python_executable=venv / "bin/python",
        production_source_root=source,
        runtime_root=tmp_path / "runtime",
    )

    assert identity.source_sha == RELEASE_SHA
    assert identity.source_tree == RELEASE_TREE
    assert identity.source_root == str(source.resolve())
    assert identity.venv_prefix == str(venv.resolve())
    assert identity.venv_slot == "a"
    assert identity.worker_lock_sha256 == DIGEST
    assert identity.slot_record_sha256 == DIGEST
    assert identity.base_python_identity_sha256 == DIGEST
    assert identity.python_executable == str(Path(sys.executable).resolve())


def test_production_runtime_identity_rejects_invalid_slot_binding(
    tmp_path: Path, monkeypatch
):
    source, module, venv = _production_runtime_paths(tmp_path)

    def fail_binding(**_kwargs):
        from scripts.worker_slot_runtime import WorkerSlotError

        raise WorkerSlotError("private slot detail")

    monkeypatch.setattr(worker_main, "verify_runtime_binding", fail_binding)

    try:
        worker_main._load_runtime_identity(
            module_path=module,
            python_prefix=venv,
            python_executable=venv / "bin/python",
            production_source_root=source,
            runtime_root=tmp_path / "runtime",
        )
    except RuntimeError as exc:
        assert "A/B runtime identity" in str(exc)
        assert "private slot detail" not in str(exc)
    else:
        raise AssertionError("invalid production slot binding was accepted")


def test_production_runtime_identity_rejects_inactive_venv(
    tmp_path: Path, monkeypatch
):
    source, module, venv = _production_runtime_paths(tmp_path)
    old_venv = tmp_path / "runtime" / "worker-venvs" / "md-b" / "venv"
    old_venv.mkdir(parents=True)
    monkeypatch.setattr(
        worker_main,
        "verify_runtime_binding",
        lambda **_kwargs: _fake_runtime_binding(venv),
    )

    try:
        worker_main._load_runtime_identity(
            module_path=module,
            python_prefix=old_venv,
            python_executable=venv / "bin/python",
            production_source_root=source,
            runtime_root=tmp_path / "runtime",
        )
    except RuntimeError as exc:
        assert "active A/B venv" in str(exc)
    else:
        raise AssertionError("inactive production Worker venv was accepted")


def test_real_health_reports_runtime_probe_failure(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn="postgresql://db/app")
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)

    monkeypatch.setattr(
        worker_main,
        "runtime_snapshot",
        _runtime_snapshot(ready=False, error="runtime import and CUDA probe exited with code 1"),
    )

    response = asyncio.run(worker_main._build_health_response())

    assert response.status == "degraded"
    assert response.mode == "real"
    assert response.db_configured is True
    assert response.byteff2_root_exists is True
    assert response.runtime_ready is False
    assert response.runtime_error == "runtime import and CUDA probe exited with code 1"


def test_real_health_reports_missing_gmx_after_import_probe(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn="postgresql://db/app")
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(
        worker_main,
        "runtime_snapshot",
        _runtime_snapshot(ready=False, error="gmx probe exited with code 127"),
    )

    response = asyncio.run(worker_main._build_health_response())

    assert response.status == "degraded"
    assert response.runtime_ready is False
    assert "gmx" in (response.runtime_error or "")
    assert response.protocols["Density"]["runtime_ready"] is False
    assert "gmx" in response.protocols["Density"]["runtime_error"]


def test_real_health_reports_missing_configured_demo_entry(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn="postgresql://db/app")
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setenv("BYTEFF2_DENSITY_DEMO_ENTRY", "missing_demo.py")

    monkeypatch.setattr(
        worker_main,
        "runtime_snapshot",
        _runtime_snapshot(
            ready=False,
            error="BYTEFF2_DENSITY_DEMO_ENTRY does not exist",
        ),
    )

    response = asyncio.run(worker_main._build_health_response())

    assert response.status == "degraded"
    assert response.runtime_ready is False
    assert "BYTEFF2_DENSITY_DEMO_ENTRY" in (response.runtime_error or "")


def test_submit_rejects_unready_formal_protocol(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn="postgresql://db/app")
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "active_jobs", {})
    monkeypatch.setattr(
        worker_main,
        "runtime_snapshot",
        _runtime_snapshot(transport_ready=False),
    )

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


def test_submit_accepts_ready_transport_with_a_stub_runner(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(
        tmp_path,
        mode="real",
        app_postgres_dsn="postgresql://db/app",
    )
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "runtime_snapshot", _runtime_snapshot())
    monkeypatch.setattr(worker_main, "active_jobs", {})
    monkeypatch.setattr(worker_main, "active_formal_jobs", set())
    monkeypatch.setattr(worker_main, "recovery_ready", True)
    monkeypatch.setattr(worker_main, "draining", False)

    async def updated(*_args, **_kwargs):
        return JobUpdateResult.UPDATED

    async def stub_run(*_args, **_kwargs):
        await asyncio.Future()

    monkeypatch.setattr(worker_main, "_safe_update_status", updated)
    monkeypatch.setattr(worker_main, "_run_job", stub_run)
    request = JobRequest(
        job_id="formal-transport-ready",
        smiles="{}",
        canonical_smiles="{}",
        steps=15_000_000,
        protocol="Transport",
        run_mode="formal",
        config_json={"protocol": "Transport"},
    )

    async def scenario() -> None:
        accepted = await worker_main.submit_job(request)
        assert accepted.status == "submitted"
        task = worker_main.active_jobs[request.job_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_submit_rejects_when_active_capacity_is_full(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, app_postgres_dsn=None, max_active_jobs=1)
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "active_jobs", {"busy-job": object()})

    client = TestClient(worker_main.app)
    response = client.post(
        "/jobs",
        json={"job_id": "job-1", "smiles": "CCO", "canonical_smiles": "CCO", "steps": 300},
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
    monkeypatch.setattr(worker_main, "runtime_snapshot", _runtime_snapshot())

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
    monkeypatch.setattr(worker_main, "runtime_snapshot", _runtime_snapshot())

    client = TestClient(worker_main.app)
    response = client.post(
        "/jobs",
        json={"job_id": "job-1", "smiles": "CCO", "canonical_smiles": "CCO", "steps": 300},
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
            return JobUpdateResult.MISSING

    monkeypatch.setattr(worker_main, "repository", MissingRowRepository())

    client = TestClient(worker_main.app)
    response = client.post(
        "/jobs",
        json={"job_id": "job-1", "smiles": "CCO", "canonical_smiles": "CCO", "steps": 300},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "monomer MD job row is not available for status updates"
    assert worker_main.active_jobs == {}


def test_formal_runner_writes_config_and_parses_density_result(tmp_path: Path):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn=None)
    settings.byteff2_root.mkdir()
    object.__setattr__(settings, "byteff2_python", sys.executable)
    openmm_dir = tmp_path / "openmm"
    for relative_path in REQUIRED_OPENMM_FILES:
        path = openmm_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    object.__setattr__(settings, "byteff2_openmm_dir", openmm_dir)
    run_md = settings.byteff2_root / "example" / "4_MD_simulations" / "run_md.py"
    run_md.parent.mkdir(parents=True)
    run_md.write_text(
        "\n".join(
            [
                "import argparse, json, os, pathlib",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--config')",
                "args = parser.parse_args()",
                "config = json.loads(pathlib.Path(args.config).read_text())",
                "out = pathlib.Path(config['output_dir'])",
                "out.mkdir(parents=True, exist_ok=True)",
                "(out / 'density_results.json').write_text(json.dumps({"
                "'density': 1.02, 'density_std': 0.01, "
                "'openmm_dir': os.environ.get('OPENMM_DIR'), "
                "'openmm_plugin_dir': os.environ.get('OPENMM_PLUGIN_DIR'), "
                "'ld_library_path': os.environ.get('LD_LIBRARY_PATH')}))",
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

    result = asyncio.run(runner.run(request, tmp_path / "job-root"))

    config = (tmp_path / "job-root" / "config.json").read_text(encoding="utf-8")
    assert str(tmp_path / "job-root" / "outputs") in config
    assert result.completed_steps == 1500000
    assert result.result["summary"]["density"] == 1.02
    assert result.result["metrics"]["density_std"] == 0.01
    assert result.result["metrics"]["openmm_dir"] == str(openmm_dir)
    assert result.result["metrics"]["openmm_plugin_dir"] == str(
        openmm_dir / "lib/plugins"
    )
    assert result.result["metrics"]["ld_library_path"].split(":")[:2] == [
        str(openmm_dir / "lib"),
        str(openmm_dir / "lib/plugins"),
    ]
    assert any(
        item["path"] == "outputs/density_results.json"
        for item in result.result["artifact_manifest"]["files"]
    )


def test_formal_runner_cancellation_terminates_process_group(
    tmp_path: Path, monkeypatch
):
    settings = _settings(tmp_path, mode="real", app_postgres_dsn=None)
    _install_short_process_group_grace(monkeypatch)
    settings.byteff2_root.mkdir()
    object.__setattr__(settings, "byteff2_python", sys.executable)
    run_md = settings.byteff2_root / "example" / "4_MD_simulations" / "run_md.py"
    run_md.parent.mkdir(parents=True)
    run_md.write_text(
        "\n".join(
            [
                "import argparse, json, os, pathlib, subprocess, sys, time",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--config')",
                "args = parser.parse_args()",
                "config = json.loads(pathlib.Path(args.config).read_text())",
                "out = pathlib.Path(config['output_dir'])",
                "out.mkdir(parents=True, exist_ok=True)",
                "(out / 'child.pid').write_text(str(os.getpid()))",
                "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                "(out / 'grandchild.pid').write_text(str(grandchild.pid))",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    runner = ByteFF2FormalRunner(settings)
    request = JobRequest(
        job_id="formal-cancel-1",
        smiles='{"DMC": "COC(=O)OC"}',
        canonical_smiles='{"DMC": "COC(=O)OC"}',
        steps=1500000,
        protocol="Density",
        run_mode="formal",
        config_json={
            "protocol": "Density",
            "temperature": 298,
            "natoms": 10000,
            "components": {"DMC": 1},
            "smiles": {"DMC": "COC(=O)OC"},
        },
    )
    output_dir = tmp_path / "cancel-job"

    async def scenario() -> tuple[int, int]:
        task = asyncio.create_task(runner.run(request, output_dir))
        pid_path = output_dir / "outputs" / "child.pid"
        grandchild_pid_path = output_dir / "outputs" / "grandchild.pid"
        for _ in range(100):
            if pid_path.exists() and grandchild_pid_path.exists():
                break
            await asyncio.sleep(0.02)
        assert pid_path.exists()
        pid = int(pid_path.read_text(encoding="utf-8"))
        assert grandchild_pid_path.exists()
        grandchild_pid = int(grandchild_pid_path.read_text(encoding="utf-8"))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled formal runner did not raise CancelledError")
        return pid, grandchild_pid

    child_pid, grandchild_pid = asyncio.run(scenario())
    for pid in (child_pid, grandchild_pid):
        process_state = Path(f"/proc/{pid}/stat")
        if process_state.exists():
            assert process_state.read_text(encoding="utf-8").split()[2] == "Z"


def test_delete_job_artifacts_removes_output_directory(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, app_postgres_dsn=None)
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "runner", worker_main.MonomerMdRunner(settings))
    monkeypatch.setattr(worker_main, "active_jobs", {})
    fsync_directories: list[Path] = []
    original_fsync_directory = worker_main._fsync_directory

    def record_fsync(path: Path) -> None:
        fsync_directories.append(path)
        original_fsync_directory(path)

    monkeypatch.setattr(worker_main, "_fsync_directory", record_fsync)
    output_dir = settings.job_root / "job-1"
    output_dir.mkdir(parents=True)
    (output_dir / "density_demo_results.json").write_text("{}", encoding="utf-8")

    client = TestClient(worker_main.app)
    response = client.delete("/jobs/job-1/artifacts")

    assert response.status_code == 200
    payload = response.json()
    assert payload["deleted"] is True
    assert not output_dir.exists()
    assert fsync_directories == [settings.job_root]

    repeated = client.delete("/jobs/job-1/artifacts")
    assert repeated.status_code == 200
    assert repeated.json()["deleted"] is False
    assert fsync_directories == [settings.job_root, settings.job_root]

    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    (outside / "must-remain.txt").write_text("preserved", encoding="utf-8")
    output_dir.symlink_to(outside, target_is_directory=True)
    symlink_cleanup = client.delete("/jobs/job-1/artifacts")
    assert symlink_cleanup.status_code == 200
    assert symlink_cleanup.json()["deleted"] is True
    assert not output_dir.exists()
    assert not output_dir.is_symlink()
    assert (outside / "must-remain.txt").read_text(encoding="utf-8") == "preserved"
    assert fsync_directories == [
        settings.job_root,
        settings.job_root,
        settings.job_root,
    ]


def test_delete_job_artifacts_refuses_active_exact_job(
    tmp_path: Path,
    monkeypatch,
):
    settings = _settings(tmp_path, app_postgres_dsn=None)
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(
        worker_main,
        "runner",
        worker_main.MonomerMdRunner(settings),
    )
    active_task = object()
    monkeypatch.setattr(worker_main, "active_jobs", {"job-1": active_task})
    output_dir = settings.job_root / "job-1"
    output_dir.mkdir(parents=True)
    (output_dir / "active.txt").write_text("active", encoding="utf-8")

    response = TestClient(worker_main.app).delete("/jobs/job-1/artifacts")

    assert response.status_code == 409
    assert (output_dir / "active.txt").read_text(encoding="utf-8") == "active"


def test_repository_update_query_guards_terminal_statuses():
    source_names = PostgresJobRepository._build_update.__code__.co_consts

    assert any(
        isinstance(value, str)
        and "NOT IN ('completed', 'failed', 'cancelled')" in value
        for value in source_names
    )


def test_drain_is_idempotent_and_rejects_new_jobs(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, app_postgres_dsn=None)
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "recovery_ready", True)
    monkeypatch.setattr(worker_main, "draining", False)
    monkeypatch.setattr(worker_main, "active_jobs", {"busy-job": object()})

    client = TestClient(worker_main.app)
    first = client.post("/drain")
    second = client.post("/drain")
    rejected = client.post(
        "/jobs",
        json={"job_id": "job-1", "smiles": "CCO", "canonical_smiles": "CCO", "steps": 300},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["active_jobs"] == 1
    assert first.json()["worker_instance_id"] == worker_main.worker_instance_id
    assert rejected.status_code == 503
    assert rejected.json()["detail"] == "monomer MD worker is draining for deployment"

    resumed = client.post("/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "ready"
    assert resumed.json()["accepting_jobs"] is False
    assert resumed.json()["active_jobs"] == 1


def test_health_accepting_jobs_respects_capacity(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, app_postgres_dsn=None, max_active_jobs=1)
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "recovery_ready", True)
    monkeypatch.setattr(worker_main, "draining", False)
    monkeypatch.setattr(worker_main, "active_jobs", {"busy-job": object()})

    response = asyncio.run(worker_main._build_health_response())

    assert response.accepting_jobs is False
    assert response.active_jobs == 1
    assert response.max_active_jobs == 1


def test_terminal_persistence_stops_when_database_is_already_terminal(monkeypatch):
    calls = []

    async def fake_update(*args, **kwargs):
        calls.append((args, kwargs))
        return JobUpdateResult.ALREADY_TERMINAL

    monkeypatch.setattr(worker_main, "_safe_update_status", fake_update)

    assert asyncio.run(worker_main._persist_terminal_status("job-1", "completed")) is True
    assert len(calls) == 1


def test_terminal_persistence_stops_when_database_row_is_missing(monkeypatch):
    calls = []

    async def fake_update(*args, **kwargs):
        calls.append((args, kwargs))
        return JobUpdateResult.MISSING

    monkeypatch.setattr(worker_main, "_safe_update_status", fake_update)

    assert asyncio.run(worker_main._persist_terminal_status("job-1", "failed")) is True
    assert len(calls) == 1


def test_terminal_persistence_retries_database_failure(monkeypatch):
    results = iter((None, JobUpdateResult.UPDATED))
    calls = []

    async def fake_update(*args, **kwargs):
        calls.append((args, kwargs))
        return next(results)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(worker_main, "_safe_update_status", fake_update)
    monkeypatch.setattr(worker_main.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(worker_main, "shutting_down", False)

    assert asyncio.run(worker_main._persist_terminal_status("job-1", "completed")) is True
    assert len(calls) == 2


def test_health_reports_capacity_instance_and_lease(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, app_postgres_dsn=None, max_active_jobs=2)
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "recovery_ready", True)
    monkeypatch.setattr(worker_main, "draining", False)
    monkeypatch.setattr(worker_main, "active_jobs", {})

    response = asyncio.run(worker_main._build_health_response())

    assert response.max_active_jobs == 2
    assert response.worker_instance_id == worker_main.worker_instance_id
    assert response.accepting_jobs is True
    assert response.draining is False
    assert response.lease_seconds == 90


def test_recovery_reconciles_previous_worker_instance(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, app_postgres_dsn="postgresql://db/app")

    class RecoveringRepository:
        def __init__(self):
            self.instances = []

        def reconcile_orphaned_jobs(self, instance_id):
            self.instances.append(instance_id)
            return 1

    fake_repository = RecoveringRepository()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "repository", fake_repository)
    monkeypatch.setattr(worker_main, "recovery_ready", False)

    recovered = asyncio.run(worker_main._attempt_recovery())

    assert recovered is True
    assert worker_main.recovery_ready is True
    assert fake_repository.instances == [worker_main.worker_instance_id]


def test_heartbeat_renews_active_instance_jobs(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, app_postgres_dsn="postgresql://db/app")
    calls = []

    class HeartbeatRepository:
        def heartbeat(self, job_ids, instance_id):
            calls.append((job_ids, instance_id))
            return len(job_ids)

    sleep_calls = 0

    async def one_iteration(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "repository", HeartbeatRepository())
    monkeypatch.setattr(worker_main, "active_jobs", {"job-1": object()})
    monkeypatch.setattr(worker_main.asyncio, "sleep", one_iteration)

    try:
        asyncio.run(worker_main._heartbeat_loop())
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("heartbeat loop did not stop after the test iteration")

    assert calls == [(["job-1"], worker_main.worker_instance_id)]


def test_empty_asyncio_timeout_is_classified_as_timeout() -> None:
    assert worker_main._classify_error(asyncio.TimeoutError()) == "timeout"
