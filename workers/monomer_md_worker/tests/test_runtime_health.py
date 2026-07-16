from __future__ import annotations

import asyncio
import json
import os
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from workers.monomer_md_worker.app.byteff2_env import REQUIRED_OPENMM_FILES
from workers.monomer_md_worker.app.config import WorkerSettings
from workers.monomer_md_worker.app.formal_protocols import FORMAL_PROTOCOLS
from workers.monomer_md_worker.app import runtime_health
from workers.monomer_md_worker.app.runtime_health import (
    ProtocolRuntimeSnapshot,
    RuntimeSnapshot,
    degraded_runtime_snapshot,
    initial_runtime_snapshot,
    probe_runtime_snapshot,
)
from workers.monomer_md_worker.app.runtime_probe import (
    TRANSPORT_PLUGIN_LOAD_FAILURE,
)


@pytest.fixture(autouse=True)
def _ready_byteff2_runtime_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_health,
        "validate_byteff2_runtime_assets",
        lambda *_args, **_kwargs: None,
    )


def _settings(
    tmp_path: Path,
    *,
    broker_enabled: bool = False,
    openmm_ready: bool = True,
    timeout_seconds: int = 30,
) -> WorkerSettings:
    byteff2_root = tmp_path / "byteff2"
    byteff2_root.mkdir(exist_ok=True)
    openmm_dir = tmp_path / "openmm"
    if openmm_ready:
        for relative_path in REQUIRED_OPENMM_FILES:
            path = openmm_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
    mps_root = tmp_path / "mps"
    pipe_dir = mps_root / "mps-1" / "pipe"
    pipe_dir.mkdir(parents=True, exist_ok=True)
    control = pipe_dir / "control"
    if not control.exists():
        os.mkfifo(control, 0o600)
    return WorkerSettings(
        mode="real",
        app_postgres_dsn="postgresql://db/app",
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
        byteff2_root=byteff2_root,
        byteff2_python="byteff2-python",
        byteff2_demo_command=None,
        job_root=tmp_path / "runs",
        default_steps=300,
        max_steps=300,
        report_interval=10,
        timeout_seconds=30,
        health_probe_timeout_seconds=timeout_seconds,
        max_concurrent_jobs=1,
        max_active_jobs=1,
        cuda_visible_devices="1",
        worker_id="test-worker",
        worker_version="test",
        byteff2_openmm_dir=openmm_dir if openmm_ready else None,
        transport_cuda_smoke_enabled=True,
        gpu_broker_enabled=broker_enabled,
        gpu_mps_pipe_root=mps_root,
        gpu_broker_environment="dev",
    )


def _managed_lease() -> SimpleNamespace:
    return SimpleNamespace(
        lease=SimpleNamespace(
            gpu_index=1,
            gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
            memory_mib=8_192,
            thread_percent=50,
            component="md",
        )
    )


class _Runner:
    def __init__(self, lease=None, *, acquire_error: Exception | None = None) -> None:
        self.lease = lease
        self.acquire_error = acquire_error
        self.acquire_calls: list[tuple[str, float]] = []
        self.release_calls: list[object] = []

    async def acquire_runtime_probe_lease(
        self, worker_instance_id: str, *, timeout_seconds: float
    ):
        self.acquire_calls.append((worker_instance_id, timeout_seconds))
        if self.acquire_error is not None:
            raise self.acquire_error
        return self.lease

    async def release_execution_lease(self, lease) -> None:
        self.release_calls.append(lease)


def _probe_stdout(
    *, transport_ready: bool = True, transport_error: str | None = None
) -> str:
    protocols = {
        protocol: {
            "supported": True,
            "runtime_ready": (
                transport_ready if protocol == "Transport" else True
            ),
            "runtime_error": (
                transport_error if protocol == "Transport" else None
            ),
        }
        for protocol in FORMAL_PROTOCOLS
    }
    return json.dumps(
        {"runtime_ready": True, "runtime_error": None, "protocols": protocols}
    )


def _completed(stdout: str = "", returncode: int = 0):
    return runtime_health._ProbeCompleted(returncode, stdout)


def test_broker_probe_uses_fenced_lease_mps_environment_and_releases_before_gmx(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, broker_enabled=True)
    lease = _managed_lease()
    runner = _Runner(lease)
    calls: list[tuple[list[str], dict[str, str], object | None]] = []

    async def fake_run(command, *, env, execution_lease, **_kwargs):
        calls.append((list(command), dict(env), execution_lease))
        if command[0] == settings.byteff2_python:
            assert runner.release_calls == []
            return _completed(_probe_stdout())
        assert command == ["gmx", "--version"]
        assert execution_lease is None
        assert runner.release_calls == [lease]
        return _completed("gmx ready")

    monkeypatch.setattr(runtime_health, "_run_probe_command", fake_run)

    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=runner,  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )

    assert snapshot.runtime_ready is True
    assert snapshot.protocols_dict()["Transport"]["runtime_ready"] is True
    assert len(runner.acquire_calls) == 1
    assert runner.acquire_calls[0][0] == "worker-instance-1"
    assert 0 < runner.acquire_calls[0][1] <= 30
    assert runner.release_calls == [lease]
    assert len(calls) == 2
    runtime_command, runtime_env, runtime_lease = calls[0]
    assert runtime_lease is lease
    assert runtime_command.count("--protocol") == len(FORMAL_PROTOCOLS)
    assert "--transport-cuda-smoke" in runtime_command
    assert runtime_env["CUDA_VISIBLE_DEVICES"] == lease.lease.gpu_uuid
    assert runtime_env["CUDA_MPS_ACTIVE_THREAD_PERCENTAGE"] == "50"
    assert runtime_env["OPENMM_DIR"] == str(settings.byteff2_openmm_dir)
    assert runtime_env["OPENMM_PLUGIN_DIR"].endswith("/lib/plugins")


def test_probe_command_uses_current_fenced_process_control_and_caps_stdout(
    tmp_path: Path, monkeypatch
) -> None:
    lease = _managed_lease()
    process = SimpleNamespace(pid=123, returncode=0)
    observed: dict[str, object] = {}

    async def fake_create(command, *, execution_lease, **kwargs):
        observed["command"] = list(command)
        observed["create_lease"] = execution_lease
        observed["stderr"] = kwargs["stderr"]
        kwargs["stdout"].write(b"x" * (runtime_health.MAX_PROBE_STDOUT_BYTES + 1))
        kwargs["stdout"].flush()
        return process

    async def fake_wait(
        waited_process, *, timeout_seconds, execution_lease
    ) -> int:
        observed["wait_process"] = waited_process
        observed["wait_timeout"] = timeout_seconds
        observed["wait_lease"] = execution_lease
        return 0

    monkeypatch.setattr(runtime_health, "create_fenced_subprocess_exec", fake_create)
    monkeypatch.setattr(runtime_health, "wait_for_process_group", fake_wait)
    monotonic_values = iter((10.0, 12.0))
    monkeypatch.setattr(runtime_health, "monotonic", lambda: next(monotonic_values))

    completed = asyncio.run(
        runtime_health._run_probe_command(
            ["probe-python", "runtime_probe.py"],
            cwd=tmp_path,
            env={"SAFE": "1"},
            deadline=17.5,
            execution_lease=lease,  # type: ignore[arg-type]
        )
    )

    assert observed["command"] == ["probe-python", "runtime_probe.py"]
    assert observed["create_lease"] is lease
    assert observed["wait_lease"] is lease
    assert observed["wait_process"] is process
    assert observed["wait_timeout"] == 5.5
    assert observed["stderr"] == asyncio.subprocess.DEVNULL
    assert completed.stdout_oversized is True
    assert len(completed.stdout) == runtime_health.MAX_PROBE_STDOUT_BYTES


def test_probe_command_does_not_reuse_budget_spent_during_registration(
    tmp_path: Path, monkeypatch
) -> None:
    lease = _managed_lease()
    process = SimpleNamespace(pid=123, returncode=None)
    terminated: list[tuple[object, object]] = []

    async def fake_create(*_args, **_kwargs):
        return process

    async def fake_terminate(candidate, *, execution_lease):
        terminated.append((candidate, execution_lease))

    async def forbidden_wait(*_args, **_kwargs):
        raise AssertionError("expired runtime process must not enter execution wait")

    monotonic_values = iter((0.0, 1.01))
    monkeypatch.setattr(runtime_health, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(runtime_health, "create_fenced_subprocess_exec", fake_create)
    monkeypatch.setattr(runtime_health, "terminate_process_group", fake_terminate)
    monkeypatch.setattr(runtime_health, "wait_for_process_group", forbidden_wait)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            runtime_health._run_probe_command(
                ["probe-python", "runtime_probe.py"],
                cwd=tmp_path,
                env={"SAFE": "1"},
                deadline=1.0,
                execution_lease=lease,  # type: ignore[arg-type]
            )
        )

    assert terminated == [(process, lease)]


def test_broker_lease_failure_never_spawns_runtime(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, broker_enabled=True)
    runner = _Runner(acquire_error=RuntimeError("private broker socket path"))

    async def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("runtime child must not start without a lease")

    monkeypatch.setattr(runtime_health, "_run_probe_command", forbidden_spawn)

    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=runner,  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )

    assert snapshot.runtime_ready is False
    assert snapshot.runtime_error == (
        "runtime startup probe could not acquire a governed GPU lease"
    )
    assert "private broker" not in (snapshot.runtime_error or "")
    assert runner.release_calls == []


def test_missing_formal_runtime_asset_degrades_all_protocols_before_cuda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)

    monkeypatch.setattr(
        runtime_health,
        "validate_byteff2_runtime_assets",
        lambda *_args, **_kwargs: (
            "required ByteFF2 runtime asset is missing: optimal.pt"
        ),
    )

    async def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("CUDA probe must not start with incomplete formal assets")

    monkeypatch.setattr(runtime_health, "_run_probe_command", forbidden_spawn)

    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=_Runner(),  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )

    assert snapshot.runtime_ready is False
    assert snapshot.runtime_error == (
        "required ByteFF2 runtime asset is missing: optimal.pt"
    )
    assert all(not item.supported for item in snapshot.protocols)
    assert all(not item.runtime_ready for item in snapshot.protocols)


def test_runtime_failure_releases_lease_and_does_not_start_gmx(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, broker_enabled=True)
    lease = _managed_lease()
    runner = _Runner(lease)
    commands: list[list[str]] = []

    async def fake_run(command, **_kwargs):
        commands.append(list(command))
        return _completed("private stderr-like detail", returncode=9)

    monkeypatch.setattr(runtime_health, "_run_probe_command", fake_run)

    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=runner,  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )

    assert snapshot.runtime_ready is False
    assert snapshot.runtime_error == (
        "runtime import and CUDA probe exited with code 9"
    )
    assert "private" not in (snapshot.runtime_error or "")
    assert runner.release_calls == [lease]
    assert len(commands) == 1


def test_cancelling_runtime_probe_releases_lease(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, broker_enabled=True)
    lease = _managed_lease()
    runner = _Runner(lease)
    started = asyncio.Event()

    async def blocking_run(*_args, **_kwargs):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(runtime_health, "_run_probe_command", blocking_run)

    async def scenario() -> None:
        task = asyncio.create_task(
            probe_runtime_snapshot(
                settings,
                runner=runner,  # type: ignore[arg-type]
                worker_instance_id="worker-instance-1",
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert runner.release_calls == [lease]


def test_shared_deadline_skips_gmx_after_runtime_stage(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, timeout_seconds=10)
    runner = _Runner()
    commands: list[list[str]] = []

    async def fake_run(command, **_kwargs):
        commands.append(list(command))
        return _completed(_probe_stdout())

    monotonic_values = iter((0.0, 0.0, 9.0, 10.01))
    monkeypatch.setattr(runtime_health, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(runtime_health, "_run_probe_command", fake_run)

    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=runner,  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )

    assert len(commands) == 1
    assert snapshot.runtime_ready is False
    assert snapshot.runtime_error == (
        "runtime startup probe exceeded total 10s budget during gmx probe"
    )


def test_broker_release_time_is_charged_to_the_shared_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, broker_enabled=True, timeout_seconds=10)
    lease = _managed_lease()
    runner = _Runner(lease)

    async def fake_run(*_args, **_kwargs):
        return _completed(_probe_stdout())

    monotonic_values = iter((0.0, 0.0, 1.0, 10.01))
    monkeypatch.setattr(runtime_health, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(runtime_health, "_run_probe_command", fake_run)

    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=runner,  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )

    assert runner.release_calls == [lease]
    assert snapshot.runtime_ready is False
    assert snapshot.runtime_error == (
        "runtime startup probe exceeded total 10s budget during "
        "GPU lease cleanup probe"
    )


@pytest.mark.parametrize("missing_file", REQUIRED_OPENMM_FILES)
def test_missing_static_openmm_asset_only_degrades_transport_when_common_probe_works(
    tmp_path: Path, monkeypatch, missing_file: Path
) -> None:
    settings = _settings(tmp_path)
    assert settings.byteff2_openmm_dir is not None
    (settings.byteff2_openmm_dir / missing_file).unlink()
    commands: list[list[str]] = []

    async def fake_run(command, *, env, **_kwargs):
        commands.append(list(command))
        assert env["OPENMM_DIR"] == str(settings.byteff2_openmm_dir)
        if command[0] == settings.byteff2_python:
            assert "--transport-cuda-smoke" not in command
            return _completed(_probe_stdout())
        return _completed("gmx ready")

    monkeypatch.setattr(runtime_health, "_run_probe_command", fake_run)

    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=_Runner(),  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )
    protocols = snapshot.protocols_dict()

    assert snapshot.runtime_ready is True
    assert protocols["Density"]["runtime_ready"] is True
    assert protocols["Transport"]["runtime_ready"] is False
    assert missing_file.name in protocols["Transport"]["runtime_error"]
    assert len(commands) == 2


def test_invalid_openmm_root_does_not_block_other_protocols(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path, openmm_ready=False)
    monkeypatch.delenv("OPENMM_DIR", raising=False)
    monkeypatch.delenv("OPENMM_PLUGIN_DIR", raising=False)

    async def fake_run(command, *, env, **_kwargs):
        assert "OPENMM_DIR" not in env
        if command[0] == settings.byteff2_python:
            return _completed(_probe_stdout())
        return _completed("gmx ready")

    monkeypatch.setattr(runtime_health, "_run_probe_command", fake_run)

    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=_Runner(),  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )

    assert snapshot.runtime_ready is True
    assert snapshot.protocols_dict()["Density"]["runtime_ready"] is True
    assert snapshot.protocols_dict()["Transport"]["runtime_ready"] is False


def test_related_plugin_failure_is_safe_and_only_degrades_transport(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)

    async def fake_run(command, **_kwargs):
        if command[0] == settings.byteff2_python:
            return _completed(
                _probe_stdout(
                    transport_ready=False,
                    transport_error=TRANSPORT_PLUGIN_LOAD_FAILURE,
                )
            )
        return _completed("gmx ready")

    monkeypatch.setattr(runtime_health, "_run_probe_command", fake_run)

    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=_Runner(),  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )
    protocols = snapshot.protocols_dict()

    assert snapshot.runtime_ready is True
    assert protocols["Transport"]["runtime_ready"] is False
    assert protocols["Transport"]["runtime_error"] == TRANSPORT_PLUGIN_LOAD_FAILURE
    assert all(
        item["runtime_ready"] is True
        for name, item in protocols.items()
        if name != "Transport"
    )


def test_public_cuda_failure_degrades_all_protocols_and_hides_output(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    commands: list[list[str]] = []

    async def fake_run(command, **_kwargs):
        commands.append(list(command))
        return _completed("/private/openmm/plugin/path", returncode=1)

    monkeypatch.setattr(runtime_health, "_run_probe_command", fake_run)

    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=_Runner(),  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )

    assert snapshot.runtime_ready is False
    assert "private" not in (snapshot.runtime_error or "")
    assert len(commands) == 1
    assert all(
        item["runtime_ready"] is False
        for item in snapshot.protocols_dict().values()
    )


def test_initial_and_degraded_snapshots_are_frozen_and_bounded(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initial = initial_runtime_snapshot(settings)
    degraded = degraded_runtime_snapshot(settings, "x" * 800)

    assert initial.runtime_ready is False
    assert tuple(item.protocol for item in initial.protocols) == FORMAL_PROTOCOLS
    assert len(degraded.runtime_error or "") == 500
    with pytest.raises(FrozenInstanceError):
        initial.runtime_ready = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        initial.protocols[0].runtime_ready = True  # type: ignore[misc]


def test_dry_run_snapshot_does_not_acquire_or_spawn(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, broker_enabled=True)
    object.__setattr__(settings, "mode", "dry-run")
    runner = _Runner(_managed_lease())

    async def forbidden_spawn(*_args, **_kwargs):
        raise AssertionError("dry-run must not spawn runtime probes")

    monkeypatch.setattr(runtime_health, "_run_probe_command", forbidden_spawn)
    snapshot = asyncio.run(
        probe_runtime_snapshot(
            settings,
            runner=runner,  # type: ignore[arg-type]
            worker_instance_id="worker-instance-1",
        )
    )

    assert snapshot.runtime_ready is True
    assert snapshot.protocols == ()
    assert runner.acquire_calls == []
    assert runner.release_calls == []


def test_snapshot_protocol_order_comes_only_from_formal_protocols() -> None:
    snapshot = RuntimeSnapshot(
        True,
        True,
        None,
        tuple(ProtocolRuntimeSnapshot(name, True, True, None) for name in FORMAL_PROTOCOLS),
    )

    assert tuple(snapshot.protocols_dict()) == FORMAL_PROTOCOLS
