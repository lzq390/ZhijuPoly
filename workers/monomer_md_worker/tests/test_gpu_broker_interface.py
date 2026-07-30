from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from gpu_resource import GpuBrokerClientError
from workers.monomer_md_worker.app import (
    byteff2_formal_runner as byteff2_formal_runner_module,
)
from workers.monomer_md_worker.app.byteff2_env import REQUIRED_OPENMM_FILES
from workers.monomer_md_worker.app.byteff2_formal_runner import ByteFF2FormalRunner
from workers.monomer_md_worker.app.config import WorkerSettings, load_settings
from workers.monomer_md_worker.app.formal_protocols import sanitize_formal_config
from workers.monomer_md_worker.app.models import JobRequest
from workers.monomer_md_worker.app.runner import MonomerMdRunner
from workers.monomer_md_worker.app.runtime_health import (
    ProtocolRuntimeSnapshot,
    RuntimeSnapshot,
)
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
    def __init__(
        self,
        *,
        gpu_index: int = 3,
        client_id: str = "md-dev-66c38bf892cc7dbe",
        request_id: str = "md:dev:66c38bf892cc7dbe",
    ) -> None:
        self.lease = SimpleNamespace(
            lease_id="1" * 32,
            fencing_token=42,
            broker_instance_id="broker-1",
            kind="execution",
            placement="any",
            environment="dev",
            client_id=client_id,
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
            request_id=request_id,
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

    monkeypatch.setenv("MONOMER_MD_MAX_CONCURRENT_JOBS", "1")
    monkeypatch.setenv("MONOMER_MD_MAX_ACTIVE_JOBS", "3")
    with pytest.raises(ValueError, match="host-only"):
        load_settings()

    monkeypatch.setenv(
        "MONOMER_MD_GPU_SCOPE_LAUNCHER", "container-host-bus-bind"
    )
    with pytest.raises(ValueError, match="systemd-user-scope"):
        load_settings()

    monkeypatch.setenv(
        "MONOMER_MD_GPU_SCOPE_LAUNCHER", "systemd-user-scope"
    )
    assert load_settings().gpu_broker_enabled is True


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


def test_formal_acquire_deadline_covers_wait_acquire_and_activate_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TimedClient(_BrokerClient):
        timeout_seconds = 7.0

    client = TimedClient(_ManagedLease(gpu_index=1))
    settings = _settings(tmp_path)
    object.__setattr__(settings, "gpu_broker_wait_timeout_seconds", 600)
    runner = MonomerMdRunner(settings, gpu_broker_client=client)  # type: ignore[arg-type]
    captured: dict[str, object] = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return client.lease

    monkeypatch.setattr(runner, "_acquire_md_execution_lease", capture)

    assert asyncio.run(runner.acquire_execution_lease("formal-1")) is client.lease
    assert captured["local_timeout_seconds"] == 616.0


def test_runner_freezes_one_subprocess_environment_for_all_protocol_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    runner = MonomerMdRunner(settings, gpu_broker_client=_BrokerClient(_ManagedLease()))  # type: ignore[arg-type]
    frozen = runner.byteff2_environment.as_dict()

    monkeypatch.setenv("LD_LIBRARY_PATH", "/late/poison")
    monkeypatch.setenv("CUDA_MPS_PIPE_DIRECTORY", "/late/mps")
    monkeypatch.setenv("PYTHONPATH", "/late/python")

    assert runner.byteff2_environment.as_dict() == frozen
    assert runner._formal_runner._environment is runner.byteff2_environment


def test_runtime_probe_acquires_a_distinct_budgeted_execution_lease(
    tmp_path: Path,
) -> None:
    worker_instance_id = "worker-instance-1"
    token = hashlib.sha256(
        f"runtime-probe:{worker_instance_id}".encode("utf-8")
    ).hexdigest()[:16]
    client_id = f"md-dev-probe-{token}"
    managed = _ManagedLease(
        gpu_index=1,
        client_id=client_id,
        request_id=f"md:dev:probe:{token}",
    )
    client = _BrokerClient(managed)
    settings = _settings(tmp_path)
    object.__setattr__(settings, "gpu_broker_wait_timeout_seconds", 600)
    runner = MonomerMdRunner(settings, gpu_broker_client=client)  # type: ignore[arg-type]

    acquired = asyncio.run(
        runner.acquire_runtime_probe_lease(
            worker_instance_id,
            timeout_seconds=4.5,
        )
    )

    assert acquired is managed
    assert client.calls == [
        {
            "kind": "execution",
            "placement": "any",
            "component": "md",
            "environment": "dev",
            "client_id": client_id,
            "memory_mib": 8192,
            "thread_percent": 50,
            "wait_timeout_seconds": 4.5,
            "heartbeat_interval_seconds": 5,
            "request_id": f"md:dev:probe:{token}",
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


def test_runtime_probe_timeout_cancels_its_stable_broker_waiter(
    tmp_path: Path,
) -> None:
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

    worker_instance_id = "worker-instance-timeout"
    token = hashlib.sha256(
        f"runtime-probe:{worker_instance_id}".encode("utf-8")
    ).hexdigest()[:16]
    settings = _settings(tmp_path)
    object.__setattr__(settings, "gpu_broker_wait_timeout_seconds", 600)
    client = BlockingClient()
    runner = MonomerMdRunner(
        settings,
        gpu_broker_client=client,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await runner.acquire_runtime_probe_lease(
                worker_instance_id,
                timeout_seconds=0.05,
            )

    asyncio.run(scenario())
    assert started.is_set()
    assert client.cancelled_request_id == f"md:dev:probe:{token}"


def test_md_recovers_ambiguous_acquire_with_the_exact_request_id(
    tmp_path: Path,
) -> None:
    managed = _ManagedLease(gpu_index=1)

    class AmbiguousResponseClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def acquire_managed(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise GpuBrokerClientError(
                    "invalid_response",
                    "allocated response was truncated",
                )
            return managed

    client = AmbiguousResponseClient()
    runner = MonomerMdRunner(
        _settings(tmp_path),
        gpu_broker_client=client,  # type: ignore[arg-type]
    )

    acquired = asyncio.run(runner.acquire_execution_lease("formal-1"))

    assert acquired is managed
    assert len(client.calls) == 2
    assert {
        str(call["request_id"])
        for call in client.calls
    } == {"md:dev:66c38bf892cc7dbe"}
    assert client.calls[1]["wait_timeout_seconds"] == 0.0


def test_md_double_cancel_collects_lost_response_late_grant(
    tmp_path: Path,
) -> None:
    acquire_started = threading.Event()
    cancel_entered = threading.Event()
    allow_cancel_return = threading.Event()
    managed = _ManagedLease(gpu_index=1)

    class LostResponseClient:
        def __init__(self) -> None:
            self.acquire_calls: list[dict[str, object]] = []

        def acquire_managed(self, **kwargs):
            self.acquire_calls.append(kwargs)
            if len(self.acquire_calls) == 1:
                acquire_started.set()
                cancel_entered.wait(timeout=2)
                raise GpuBrokerClientError(
                    "gpu_broker_unavailable",
                    "grant response was lost",
                )
            return managed

        def cancel_acquire(self, request_id: str) -> bool:
            assert request_id == "md:dev:66c38bf892cc7dbe"
            cancel_entered.set()
            allow_cancel_return.wait(timeout=2)
            return False

    client = LostResponseClient()
    runner = MonomerMdRunner(
        _settings(tmp_path),
        gpu_broker_client=client,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        task = asyncio.create_task(runner.acquire_execution_lease("formal-1"))
        assert await asyncio.to_thread(acquire_started.wait, 1)
        task.cancel()
        assert await asyncio.to_thread(cancel_entered.wait, 1)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        allow_cancel_return.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(client.acquire_calls) >= 2
    assert all(
        call["request_id"] == "md:dev:66c38bf892cc7dbe"
        for call in client.acquire_calls
    )
    assert managed.closed is True


def test_md_lease_close_finishes_before_repeated_cancel_propagates(
    tmp_path: Path,
) -> None:
    close_started = threading.Event()
    allow_close = threading.Event()
    managed = _ManagedLease(gpu_index=1)

    def blocking_close() -> None:
        close_started.set()
        allow_close.wait(timeout=2)
        managed.closed = True

    managed.close = blocking_close  # type: ignore[method-assign]
    runner = MonomerMdRunner(
        _settings(tmp_path),
        gpu_broker_client=_BrokerClient(managed),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        task = asyncio.create_task(runner.release_execution_lease(managed))  # type: ignore[arg-type]
        assert await asyncio.to_thread(close_started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert managed.closed is True


def test_unreachable_broker_returns_after_bounded_cleanup_and_blocks_reuse(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_recovery = threading.Event()

    class UnreachableClient:
        def acquire_managed(self, **_kwargs):
            if stop_recovery.is_set():
                raise GpuBrokerClientError("acquire_cancelled", "stopped")
            raise GpuBrokerClientError("gpu_broker_unavailable", "offline")

        def cancel_acquire(self, _request_id: str) -> bool:
            raise GpuBrokerClientError("gpu_broker_unavailable", "offline")

    settings = _settings(tmp_path)
    object.__setattr__(settings, "gpu_broker_wait_timeout_seconds", 600)
    runner = MonomerMdRunner(
        settings,
        gpu_broker_client=UnreachableClient(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "workers.monomer_md_worker.app.runner.STABLE_ACQUIRE_COLLECTION_GRACE_SECONDS",
        0.1,
    )

    async def scenario() -> None:
        started = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.TimeoutError):
            await runner.acquire_runtime_probe_lease(
                "offline-probe",
                timeout_seconds=0.05,
            )
        assert asyncio.get_running_loop().time() - started < 0.5
        assert runner.gpu_admission_uncertain is True
        with pytest.raises(GpuBrokerClientError, match="unresolved ownership"):
            await runner.acquire_execution_lease("later-job")

    try:
        asyncio.run(scenario())
    finally:
        stop_recovery.set()


def test_formal_acquire_has_a_bounded_local_transport_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stop_recovery = threading.Event()

    class UnreachableClient:
        def acquire_managed(self, **_kwargs):
            if stop_recovery.is_set():
                raise GpuBrokerClientError("acquire_cancelled", "stopped")
            raise GpuBrokerClientError("gpu_broker_unavailable", "offline")

        def cancel_acquire(self, _request_id: str) -> bool:
            raise GpuBrokerClientError("gpu_broker_unavailable", "offline")

    settings = _settings(tmp_path)
    object.__setattr__(settings, "gpu_broker_wait_timeout_seconds", 0)
    runner = MonomerMdRunner(
        settings,
        gpu_broker_client=UnreachableClient(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        "workers.monomer_md_worker.app.runner.STABLE_ACQUIRE_SCHEDULING_ALLOWANCE_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        "workers.monomer_md_worker.app.runner.DEFAULT_BROKER_CLIENT_TIMEOUT_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        "workers.monomer_md_worker.app.runner.STABLE_ACQUIRE_COLLECTION_GRACE_SECONDS",
        0.1,
    )

    async def scenario() -> None:
        started = asyncio.get_running_loop().time()
        with pytest.raises(asyncio.TimeoutError):
            await runner.acquire_execution_lease("offline-formal-job")
        assert asyncio.get_running_loop().time() - started < 0.5
        assert runner.gpu_admission_uncertain is True

    try:
        asyncio.run(scenario())
    finally:
        stop_recovery.set()


def test_authoritative_cancel_blocks_post_cancel_same_id_reacquire(
    tmp_path: Path,
) -> None:
    cancel_linearized = threading.Event()
    release_cancel_response = threading.Event()

    class RaceClient:
        calls = 0
        post_cancel_ambiguous_grant = False

        def acquire_managed(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                cancel_linearized.wait(timeout=2)
                raise GpuBrokerClientError(
                    "invalid_response",
                    "first response was truncated",
                )
            self.post_cancel_ambiguous_grant = True
            release_cancel_response.wait(timeout=2)
            raise GpuBrokerClientError(
                "invalid_response",
                "post-cancel grant response was truncated",
            )

        def cancel_acquire(self, _request_id: str) -> bool:
            cancel_linearized.set()
            # Keep the authoritative True response in transit long enough to
            # expose an ownership thread that retries without a decision gate.
            time.sleep(0.05)
            release_cancel_response.set()
            return True

    client = RaceClient()
    runner = MonomerMdRunner(
        _settings(tmp_path),
        gpu_broker_client=client,  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        task = asyncio.create_task(runner.acquire_execution_lease("formal-1"))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert client.calls == 1
    assert client.post_cancel_ambiguous_grant is False
    assert runner.gpu_admission_uncertain is False


def test_broker_health_reads_snapshot_without_acquiring_a_lease(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "recovery_ready", True)
    monkeypatch.setattr(
        worker_main,
        "runtime_snapshot",
        RuntimeSnapshot(
            True,
            True,
            None,
            tuple(
                ProtocolRuntimeSnapshot(protocol, True, True, None)
                for protocol in (
                    "Density",
                    "Transport",
                    "HVap",
                    "Dielectric",
                    "Compressibility",
                )
            ),
        ),
    )

    class ReadyBroker:
        def __init__(self, _socket_path: str, *, timeout_seconds: float) -> None:
            assert timeout_seconds == worker_main.BROKER_HEALTH_CLIENT_TIMEOUT_SECONDS
            pass

        def status(self) -> dict[str, object]:
            return {"draining": False}

    class NoProbeRunner:
        async def acquire_runtime_probe_lease(self, *_args, **_kwargs):
            raise AssertionError("health must not acquire a runtime probe lease")

    monkeypatch.setattr(worker_main, "GpuBrokerClient", ReadyBroker)
    monkeypatch.setattr(worker_main, "runner", NoProbeRunner())

    response = asyncio.run(worker_main._build_health_response())

    assert response.runtime_ready is True
    assert response.protocols["Transport"]["runtime_ready"] is True
    assert response.gpu_broker_ready is True

    monkeypatch.setattr(
        worker_main,
        "runner",
        SimpleNamespace(gpu_admission_uncertain=True),
    )
    degraded = asyncio.run(worker_main._build_health_response())
    assert degraded.gpu_broker_ready is False
    assert degraded.status == "degraded"


def test_concurrent_health_reads_do_not_serialize_on_a_slow_broker(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    settings.byteff2_root.mkdir()
    monkeypatch.setattr(worker_main, "settings", settings)
    monkeypatch.setattr(worker_main, "recovery_ready", True)
    monkeypatch.setattr(worker_main, "runtime_snapshot", RuntimeSnapshot(True, True, None, ()))
    entered = threading.Barrier(10)

    class SlowBroker:
        def __init__(self, _socket_path: str, *, timeout_seconds: float) -> None:
            assert timeout_seconds == worker_main.BROKER_HEALTH_CLIENT_TIMEOUT_SECONDS

        def status(self) -> dict[str, object]:
            entered.wait(timeout=1)
            threading.Event().wait(0.5)
            return {"draining": False}

    monkeypatch.setattr(worker_main, "GpuBrokerClient", SlowBroker)

    async def scenario() -> None:
        started = asyncio.get_running_loop().time()
        responses = await asyncio.gather(
            *(worker_main._build_health_response() for _ in range(10))
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 1.0
        assert all(response.gpu_broker_ready is False for response in responses)
        assert all(response.status == "degraded" for response in responses)

    asyncio.run(scenario())


def test_formal_child_receives_leased_device_and_mps_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    settings.byteff2_root.mkdir()
    openmm_dir = tmp_path / "openmm"
    for relative_path in REQUIRED_OPENMM_FILES:
        native_asset = openmm_dir / relative_path
        native_asset.parent.mkdir(parents=True, exist_ok=True)
        native_asset.touch()
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
                "payload = {'cuda': os.environ.get('CUDA_VISIBLE_DEVICES'), 'mps': os.environ.get('CUDA_MPS_ACTIVE_THREAD_PERCENTAGE'), 'priority': os.environ.get('CUDA_MPS_CLIENT_PRIORITY'), 'memory': os.environ.get('CUDA_MPS_PINNED_DEVICE_MEM_LIMIT'), 'pipe': os.environ.get('CUDA_MPS_PIPE_DIRECTORY'), 'openmm': os.environ.get('OPENMM_DIR'), 'plugin': os.environ.get('OPENMM_PLUGIN_DIR'), 'ld': os.environ.get('LD_LIBRARY_PATH'), 'pythonpath': os.environ.get('PYTHONPATH')}",
                "(out / 'density_results.json').write_text(json.dumps(payload))",
            ]
        ),
        encoding="utf-8",
    )
    managed = _ManagedLease(gpu_index=3)
    create_fenced = byteff2_formal_runner_module.create_fenced_subprocess_exec

    async def create_portable_fenced(*args, **kwargs):
        kwargs["scope_command_builder"] = (
            lambda _lease_id, command: tuple(command)
        )
        kwargs["scope_membership_waiter"] = (
            lambda _pid, _lease_id: 1
        )
        return await create_fenced(*args, **kwargs)

    monkeypatch.setattr(
        byteff2_formal_runner_module,
        "create_fenced_subprocess_exec",
        create_portable_fenced,
    )

    result = asyncio.run(
        ByteFF2FormalRunner(settings).run(
            _formal_request(),
            tmp_path / "job",
            execution_lease=managed,  # type: ignore[arg-type]
        )
    )

    metrics = dict(result.result["metrics"])
    ld_library_path = metrics.pop("ld")
    python_path = metrics.pop("pythonpath")
    assert metrics == {
        "cuda": managed.lease.gpu_uuid,
        "mps": "50",
        "priority": "1",
        "memory": f"{managed.lease.gpu_uuid}=8192M",
        "pipe": str(settings.gpu_mps_pipe_root / "mps-3" / "pipe"),
        "openmm": str(openmm_dir),
        "plugin": str(openmm_dir / "lib" / "plugins"),
    }
    assert ld_library_path.split(os.pathsep)[:2] == [
        str(openmm_dir / "lib"),
        str(openmm_dir / "lib" / "plugins"),
    ]
    assert python_path.split(os.pathsep)[:2] == [
        str(settings.byteff2_root),
        str(settings.byteff2_root / "submodules" / "bytemol"),
    ]
    assert result.result["gpu_device"] == "3"
    assert result.result["gpu_uuid"] == managed.lease.gpu_uuid
    assert result.result["gpu_budget_mib"] == 8192
    assert result.result["gpu_fencing_token"] == 42
    assert managed.registered_workload_pid is not None
