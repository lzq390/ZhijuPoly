from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from gpu_resource import GpuBrokerClientError
from workers.monomer_md_worker.app.byteff2_formal_runner import ByteFF2FormalRunner
from workers.monomer_md_worker.app.config import WorkerSettings, load_settings
from workers.monomer_md_worker.app.formal_protocols import sanitize_formal_config
from workers.monomer_md_worker.app.models import JobRequest
from workers.monomer_md_worker.app.runner import MonomerMdRunner
from workers.monomer_md_worker.app import main as worker_main


def _settings(tmp_path: Path, *, broker_enabled: bool = True) -> WorkerSettings:
    mps_root = tmp_path / "mps"
    for index in (1, 3):
        pipe_directory = mps_root / f"mps-{index}" / "pipe"
        pipe_directory.mkdir(parents=True, exist_ok=True)
        control = pipe_directory / "control"
        if not control.exists():
            os.mkfifo(control, 0o600)
    return WorkerSettings(
        mode="real",
        app_postgres_dsn=None,
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
        byteff2_python=sys.executable,
        byteff2_demo_command=None,
        job_root=tmp_path / "runs",
        default_steps=300,
        max_steps=300,
        report_interval=10,
        timeout_seconds=30,
        health_probe_timeout_seconds=5,
        max_concurrent_jobs=1,
        max_active_jobs=1,
        cuda_visible_devices="2",
        worker_id="test-worker",
        worker_version="test",
        gpu_broker_enabled=broker_enabled,
        gpu_broker_socket_path=str(tmp_path / "broker.sock"),
        gpu_mps_pipe_root=mps_root,
        gpu_broker_environment="dev",
        gpu_broker_wait_timeout_seconds=0,
        gpu_broker_heartbeat_interval_seconds=5,
    )


class _ManagedLease:
    def __init__(self, *, gpu_index: int = 3) -> None:
        self.lease = SimpleNamespace(
            lease_id="lease-1",
            fencing_token=42,
            broker_instance_id="broker-1",
            kind="execution",
            placement="any",
            environment="dev",
            client_id="md-dev-66c38bf892cc7dbe",
            gpu_index=gpu_index,
            gpu_uuid=(
                "GPU-0818ca6b-d9b6-af6a-71bf-afe3777ee3a5"
                if gpu_index == 3
                else "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
            ),
            memory_mib=8192,
            thread_percent=50,
            component="md",
            preferred=gpu_index == 1,
            parent_lease_id=None,
            status="active",
        )
        self.lost = False
        self.closed = False
        self.termination_unsafe = False
        self.registered_workload_pid: int | None = None

    def assert_healthy(self) -> None:
        if self.lost:
            raise GpuBrokerClientError("gpu_lease_lost", "lost")

    def close(self) -> None:
        self.closed = True

    def abandon(self) -> None:
        self.closed = True

    def register_workload(self, workload_pid: int):
        self.registered_workload_pid = workload_pid
        return self.lease

    def prepare_process_termination(self) -> dict[str, object]:
        return {
            "safe_to_signal": True,
            "client_pids": [],
            "prepared_at": 1.0,
            "freeze_token": "test-freeze",
        }

    def fail_closed(self) -> None:
        self.termination_unsafe = True


class _BrokerClient:
    def __init__(self, lease: _ManagedLease) -> None:
        self.lease = lease
        self.calls: list[dict[str, object]] = []

    def acquire_managed(self, **kwargs):
        self.calls.append(kwargs)
        return self.lease


def _formal_request(natoms: int = 10_000) -> JobRequest:
    return JobRequest(
        job_id="formal-1",
        smiles='{"DMC":"COC(=O)OC"}',
        canonical_smiles='{"DMC":"COC(=O)OC"}',
        steps=1_500_000,
        protocol="Density",
        run_mode="formal",
        config_json={
            "protocol": "Density",
            "temperature": 298,
            "natoms": natoms,
            "components": {"DMC": 1},
            "smiles": {"DMC": "COC(=O)OC"},
        },
    )


def test_worker_request_and_scientific_sanitizer_both_enforce_natoms_cap() -> None:
    with pytest.raises(ValidationError, match="natoms must be <= 10000"):
        _formal_request(10_001)
    config = {
        "protocol": "Density",
        "temperature": 298,
        "natoms": 10_001,
        "components": {"DMC": 1},
        "smiles": {"DMC": "COC(=O)OC"},
    }
    with pytest.raises(ValueError, match="natoms must be <= 10000"):
        sanitize_formal_config(config, "Density", "/managed/job")


def test_broker_governed_worker_rejects_invalid_boolean_and_parallel_jobs(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MONOMER_MD_GPU_BROKER_ENABLED", "enabled-ish")
    with pytest.raises(ValueError, match="must be a boolean"):
        load_settings()

    monkeypatch.setenv("MONOMER_MD_GPU_BROKER_ENABLED", "true")
    monkeypatch.setenv("MONOMER_MD_MAX_CONCURRENT_JOBS", "2")
    monkeypatch.setenv("MONOMER_MD_MAX_ACTIVE_JOBS", "2")
    with pytest.raises(ValueError, match="Broker-governed MD requires"):
        load_settings()


def test_md_acquires_fixed_per_job_budget_and_dev_policy(tmp_path: Path) -> None:
    managed = _ManagedLease(gpu_index=1)
    client = _BrokerClient(managed)
    runner = MonomerMdRunner(_settings(tmp_path), gpu_broker_client=client)  # type: ignore[arg-type]

    acquired = asyncio.run(runner.acquire_execution_lease("formal-1"))
    assert acquired is managed
    assert client.calls == [
        {
            "kind": "execution",
            "placement": "any",
            "component": "md",
            "environment": "dev",
            "client_id": "md-dev-66c38bf892cc7dbe",
            "memory_mib": 8192,
            "thread_percent": 50,
            "wait_timeout_seconds": 0.0,
            "heartbeat_interval_seconds": 5,
            "request_id": "md:dev:66c38bf892cc7dbe",
        }
    ]


def test_md_cancel_explicitly_removes_stable_broker_waiter(tmp_path: Path) -> None:
    started = threading.Event()
    cancelled = threading.Event()

    class BlockingClient:
        cancelled_request_id: str | None = None

        def acquire_managed(self, **_kwargs):
            started.set()
            cancelled.wait(timeout=2)
            raise GpuBrokerClientError("acquire_cancelled", "cancelled")

        def cancel_acquire(self, request_id: str) -> bool:
            self.cancelled_request_id = request_id
            cancelled.set()
            return True

    client = BlockingClient()
    runner = MonomerMdRunner(
        _settings(tmp_path), gpu_broker_client=client  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        task = asyncio.create_task(runner.acquire_execution_lease("formal-1"))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert client.cancelled_request_id == "md:dev:66c38bf892cc7dbe"


def test_broker_health_probe_is_cpu_filesystem_only(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    settings.byteff2_root.mkdir()
    run_md = settings.byteff2_root / "example" / "4_MD_simulations" / "run_md.py"
    run_md.parent.mkdir(parents=True)
    run_md.write_text("# delivered\n", encoding="utf-8")
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.delenv("BYTEFF2_DENSITY_DEMO_ENTRY", raising=False)
    monkeypatch.setattr(
        worker_main.shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        worker_main.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Broker health must not launch runtime/CUDA probes")
        ),
    )

    ready, error, protocols = worker_main._probe_real_runtime()

    assert ready is True
    assert error is None
    assert all(item["probe"] == "lease_gated_at_execution" for item in protocols.values())


def test_formal_child_receives_leased_device_and_mps_cap(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.byteff2_root.mkdir()
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
                "payload = {'cuda': os.environ.get('CUDA_VISIBLE_DEVICES'), 'mps': os.environ.get('CUDA_MPS_ACTIVE_THREAD_PERCENTAGE'), 'priority': os.environ.get('CUDA_MPS_CLIENT_PRIORITY'), 'memory': os.environ.get('CUDA_MPS_PINNED_DEVICE_MEM_LIMIT'), 'pipe': os.environ.get('CUDA_MPS_PIPE_DIRECTORY')}",
                "(out / 'density_results.json').write_text(json.dumps(payload))",
            ]
        ),
        encoding="utf-8",
    )
    managed = _ManagedLease(gpu_index=3)

    result = asyncio.run(
        ByteFF2FormalRunner(settings).run(
            _formal_request(),
            tmp_path / "job",
            execution_lease=managed,  # type: ignore[arg-type]
        )
    )

    assert result.result["metrics"] == {
        "cuda": managed.lease.gpu_uuid,
        "mps": "50",
        "priority": "1",
        "memory": f"{managed.lease.gpu_uuid}=8192M",
        "pipe": str(settings.gpu_mps_pipe_root / "mps-3" / "pipe"),
    }
    assert result.result["gpu_device"] == "3"
    assert result.result["gpu_uuid"] == managed.lease.gpu_uuid
    assert result.result["gpu_budget_mib"] == 8192
    assert result.result["gpu_fencing_token"] == 42
    assert managed.registered_workload_pid is not None
