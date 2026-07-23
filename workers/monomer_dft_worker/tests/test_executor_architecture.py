from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gpu_resource import transient_scope_command
from workers.monomer_dft_worker.app import executor_pool as executor_pool_module
from workers.monomer_dft_worker.app import gpu_broker_client as gpu_broker_client_module
from workers.monomer_dft_worker.app.artifacts import (
    atomic_write_json,
    describe_artifact,
    sha256_file,
)
from workers.monomer_dft_worker.app.config import WorkerSettings
from workers.monomer_dft_worker.app.engine import (
    ComputationCancelled,
    EngineExecution,
    ScientificComputationError,
)
from workers.monomer_dft_worker.app.executor_ipc import (
    ExecutorProtocolError,
    protocol_message,
    receive_frame,
    send_frame,
)
from workers.monomer_dft_worker.app.executor_pool import ExecutorPool, SubprocessExecutor
from workers.monomer_dft_worker.app import executor_process
from workers.monomer_dft_worker.app.gpu_broker_client import (
    DisabledBrokerClient,
    GpuAcquireCancelled,
    GpuCapacityUnavailable,
    GpuLease,
    GpuLeaseLost,
    GpuRuntimeUnhealthy,
    GpuTerminationUnsafe,
    SharedGpuBrokerAdapter,
    audit_isolated_gpu_availability,
    process_start_time,
)
from workers.monomer_dft_worker.app.schemas import (
    GpuExecutionProvenanceV2,
    JobSubmitRequest,
)


def _settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        python=Path(sys.executable),
        uds=tmp_path / "socket/worker.sock",
        job_root=tmp_path / "runs",
        max_concurrent_jobs=1,
        physical_gpu="1",
        logical_device="cuda:0",
        aimnet_cache_dir=tmp_path / "models",
        warp_cache_path=tmp_path / "warp",
        model_name="aimnet2",
        worker_version="test",
        deployment="dev",
        overflow_gpu_devices=("3",),
        preload_all_models=True,
        warmup_models=True,
        dev_runtime_root=tmp_path,
    )


def _request(model: str = "aimnet2") -> JobSubmitRequest:
    return JobSubmitRequest(
        schema_version=2,
        enqueue_sequence=17,
        job_id="executor-job",
        attempt_token="a" * 32,
        input={"smiles": "O", "net_charge": 0, "multiplicity": 1},
        calculation_type="single_point",
        model=model,
        conformer={"seed": 1, "max_iterations": 20},
        single_point={"properties": ["energy"]},
    )


class ScriptedBroker(DisabledBrokerClient):
    def __init__(
        self,
        *,
        blocked_execution: set[str] | None = None,
        lose_on_heartbeat: int | None = None,
        fail_prepare: bool = False,
    ) -> None:
        super().__init__()
        self.blocked_execution = set(blocked_execution or ())
        self.acquires: list[tuple[str, str, int]] = []
        self.releases: list[str] = []
        self.quarantines: list[str] = []
        self.termination_prepares: list[str] = []
        self.abandons: list[str] = []
        self.acquired_leases: list[GpuLease] = []
        self.heartbeat_calls = 0
        self.lose_on_heartbeat = lose_on_heartbeat
        self.fail_prepare = fail_prepare

    def acquire(self, **kwargs):
        self.acquires.append(
            (kwargs["kind"], kwargs["gpu_index"], kwargs["budget_mib"])
        )
        if kwargs["kind"] == "execution" and kwargs["gpu_index"] in self.blocked_execution:
            raise GpuCapacityUnavailable("scripted capacity rejection")
        lease = super().acquire(**kwargs)
        self.acquired_leases.append(lease)
        return lease

    def release(self, lease):
        self.releases.append(lease.lease_id)
        return super().release(lease)

    def quarantine(self, lease, *, reason):
        self.quarantines.append(lease.gpu_index)
        return super().quarantine(lease, reason=reason)

    def prepare_process_termination(self, lease):
        self.termination_prepares.append(lease.lease_id)
        if lease.kind == "execution" and lease.parent_lease_id is not None:
            raise GpuTerminationUnsafe(
                "a parented execution lease has no process termination authority"
            )
        if self.fail_prepare:
            raise RuntimeError("scripted MPS termination failure")
        return super().prepare_process_termination(lease)

    def abandon(self, lease):
        self.abandons.append(lease.lease_id)
        return super().abandon(lease)

    def heartbeat(self, lease):
        self.heartbeat_calls += 1
        if self.heartbeat_calls == self.lose_on_heartbeat:
            raise GpuLeaseLost("scripted fencing")
        return super().heartbeat(lease)


class FakeHandle:
    def __init__(
        self,
        *,
        lease,
        mode,
        model,
        failure=None,
        start_failure=None,
        calls=None,
        **_kwargs,
    ) -> None:
        self.lease = lease
        self.mode = mode
        self.model = model
        self.failure = failure
        self.start_failure = start_failure
        self.calls = calls if calls is not None else []
        self.pid = os.getpid()
        self.model_load_ms = 9.0 if mode == "overflow" else 30.0
        aliases = (
            "aimnet2",
            "aimnet2-b973c",
            "aimnet2-2025",
            "aimnet2-nse",
            "aimnet2-pd",
            "aimnet2-rxn",
        )
        self.probe_payload = {
            "ready": True,
            "model_loaded": True,
            "model_name": "aimnet2",
            "models": {
                alias: {
                    "loaded": True,
                    "registry_key": f"{alias}-registry",
                    "family": f"{alias}-family",
                    "sha256": "a" * 64,
                }
                for alias in aliases
            },
            "visible_gpu_count": 1,
            "logical_device": "cuda:0",
            "gpu_uuid": lease.gpu_uuid,
            "gpu_name": f"Fake GPU {lease.gpu_index}",
            "torch_version": "test-torch",
            "cuda_runtime": "test-cuda",
        }
        self.broken = False
        self.closed = 0

    def start(self, activate=None, prepare_termination=None) -> None:
        del prepare_termination
        self.calls.append(("start", self.mode, self.lease.gpu_index, self.model))
        if activate is not None:
            activate(self.pid)
        if self.start_failure is not None:
            raise self.start_failure

    def execute(self, request, output_directory, *, identity, provenance, **_kwargs):
        self.calls.append(("execute", self.mode, self.lease.gpu_index, identity, provenance))
        if self.failure is not None:
            self.broken = True
            raise self.failure
        output_directory.mkdir(parents=True, exist_ok=True)
        result = {"schema_version": 2, "provenance": provenance}
        result_path = output_directory / "scientific_result.json"
        atomic_write_json(result_path, result)
        descriptor = describe_artifact(
            artifact_id="scientific_result",
            path=result_path,
            media_type="application/json",
        )
        return EngineExecution(
            result=result,
            timings={"total_ms": 1.0},
            artifacts=((descriptor, result_path),),
        )

    def close(self, *, force=False, prepare_termination=None) -> None:
        self.closed += 1
        self.calls.append(("close", self.mode, force))
        if force and prepare_termination is not None:
            prepare_termination()


class HandleFactory:
    def __init__(self, failure=None, *, overflow_start_failure=None) -> None:
        self.failure = failure
        self.overflow_start_failure = overflow_start_failure
        self.calls: list[Any] = []
        self.handles: list[FakeHandle] = []

    def __call__(self, **kwargs):
        handle = FakeHandle(
            failure=self.failure,
            start_failure=(
                self.overflow_start_failure
                if kwargs.get("mode") == "overflow"
                else None
            ),
            calls=self.calls,
            **kwargs,
        )
        self.handles.append(handle)
        return handle


def test_supervisor_import_path_is_cuda_library_free() -> None:
    code = """
import json, sys
import workers.monomer_dft_worker.app.main
blocked = [name for name in sys.modules if name == 'torch' or name.startswith('torch.') or name == 'warp' or name.startswith('warp.') or name == 'aimnet' or name.startswith('aimnet.')]
print(json.dumps(blocked))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert json.loads(completed.stdout) == []


def test_executor_ipc_is_strict_versioned_json() -> None:
    left, right = socket.socketpair()
    try:
        message = protocol_message("health", finite=1.5)
        send_frame(left, message)
        assert receive_frame(right) == message
        with pytest.raises(ExecutorProtocolError, match="strict JSON"):
            send_frame(left, {"value": float("nan")})
        with pytest.raises(ExecutorProtocolError, match="strict JSON"):
            send_frame(left, {"value": object()})
    finally:
        left.close()
        right.close()


def test_process_start_time_parses_proc_comm_with_spaces_and_parentheses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc_fields_3_through_22 = ["S", *[str(value) for value in range(4, 23)]]
    payload = "4321 (executor name (gpu 1)) " + " ".join(
        proc_fields_3_through_22
    )
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: payload)

    assert process_start_time(4321) == 22


@pytest.mark.parametrize("selector", ("1", "GPU-test-authorized"))
def test_executor_entrypoint_accepts_index_or_broker_authorized_uuid_selector(
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    parent, child = socket.socketpair()
    served: list[tuple[str, str, str]] = []

    def fake_serve(_stream, *, mode: str, model: str, gpu_index: str) -> int:
        served.append((mode, model, gpu_index))
        return 0

    monkeypatch.setenv("NEXPOLY_DFT_EXECUTOR_GPU_DEVICE", "1")
    monkeypatch.setenv("NEXPOLY_DFT_EXECUTOR_GPU_UUID", "GPU-test-authorized")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", selector)
    monkeypatch.setenv("MONOMER_DFT_EXECUTOR_PROCESS", "1")
    monkeypatch.setattr(executor_process, "_serve", fake_serve)
    try:
        assert executor_process.main(
            [
                "--fd",
                str(child.detach()),
                "--mode",
                "primary",
                "--model",
                "aimnet2",
                "--gpu-index",
                "1",
            ]
        ) == 0
    finally:
        parent.close()

    assert served == [("primary", "aimnet2", "1")]


def test_executor_entrypoint_rejects_unfenced_uuid_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, child = socket.socketpair()
    monkeypatch.setenv("NEXPOLY_DFT_EXECUTOR_GPU_DEVICE", "1")
    monkeypatch.setenv("NEXPOLY_DFT_EXECUTOR_GPU_UUID", "GPU-test-authorized")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "GPU-other")
    monkeypatch.setenv("MONOMER_DFT_EXECUTOR_PROCESS", "1")
    try:
        with pytest.raises(RuntimeError, match="GPU was not selected"):
            executor_process.main(
                [
                    "--fd",
                    str(child.fileno()),
                    "--mode",
                    "primary",
                    "--model",
                    "aimnet2",
                    "--gpu-index",
                    "1",
                ]
            )
    finally:
        parent.close()
        child.close()


@pytest.mark.parametrize("shared_mps", (False, True))
def test_real_subprocess_start_handshake_proves_selector_and_cuda_uuid(
    tmp_path: Path,
    shared_mps: bool,
) -> None:
    systemd_run = tmp_path / "bin" / "systemd-run"
    systemd_run.parent.mkdir()
    scope_arguments = tmp_path / "scope-arguments"
    systemd_run.write_text(
        f"""#!{sys.executable}
import os
from pathlib import Path
import sys

Path({str(scope_arguments)!r}).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
separator = sys.argv.index("--")
target = sys.argv[separator + 1:]
os.execv(target[0], target)
""",
        encoding="utf-8",
    )
    systemd_run.chmod(0o700)
    executable = tmp_path / "fake-executor-python"
    executable.write_text(
        f"""#!{sys.executable}
import argparse
import os
import socket
import sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[3])!r})
from workers.monomer_dft_worker.app.executor_ipc import protocol_message, receive_frame, send_frame

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--fd', type=int, required=True)
parser.add_argument('--mode', required=True)
parser.add_argument('--model', required=True)
parser.add_argument('--gpu-index', required=True)
args, _ = parser.parse_known_args()
stream = socket.socket(fileno=args.fd)
gpu_uuid = os.environ['NEXPOLY_DFT_EXECUTOR_GPU_UUID']
send_frame(stream, protocol_message(
    'spawned', pid=os.getpid(), mode=args.mode, model=args.model,
    gpu_index=args.gpu_index, gpu_uuid=gpu_uuid,
    expected_gpu_uuid=gpu_uuid,
    cuda_visible_devices=os.environ['CUDA_VISIBLE_DEVICES'],
))
authorization = receive_frame(stream)
assert authorization['type'] == 'authorize_cuda'
send_frame(stream, protocol_message(
    'ready', pid=os.getpid(), mode=args.mode, model=args.model,
    gpu_index=args.gpu_index, gpu_uuid=gpu_uuid,
    cuda_visible_devices=os.environ['CUDA_VISIBLE_DEVICES'],
    model_load_ms=1.0,
    probe={{'ready': True, 'gpu_uuid': gpu_uuid, 'visible_gpu_count': 1, 'logical_device': 'cuda:0', 'models': {{}}}},
))
command = receive_frame(stream)
assert command['type'] == 'shutdown'
send_frame(stream, protocol_message('stopped'))
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    gpu_uuid = "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771"
    client_environment = (
        (("CUDA_VISIBLE_DEVICES", gpu_uuid),) if shared_mps else ()
    )
    lease = GpuLease(
        lease_id="d" * 32,
        gpu_index="1",
        gpu_uuid=gpu_uuid,
        kind="residency",
        budget_mib=4096,
        active_thread_percentage=50,
        fencing_token=1,
        preferred=True,
        broker_instance_id="broker-handshake",
        client_environment=client_environment,
    )
    executor = SubprocessExecutor(
        settings=dataclasses.replace(_settings(tmp_path), python=executable),
        lease=lease,
        mode="primary",
        model="aimnet2",
        scope_command_builder=lambda lease_id, command: transient_scope_command(
            lease_id,
            command,
            systemd_run=systemd_run,
        ),
    )

    executor.start()
    try:
        assert executor.probe_payload["gpu_uuid"] == gpu_uuid
        assert executor.pid > 0
        if shared_mps:
            arguments = scope_arguments.read_text(encoding="utf-8").splitlines()
            assert arguments == [
                "--user",
                "--scope",
                "--quiet",
                "--no-ask-password",
                f"--unit=nexpoly-gpu-job-{lease.lease_id}.scope",
                "--slice=nexpoly-gpu-jobs.slice",
                "--property=KillMode=control-group",
                "--property=CollectMode=inactive-or-failed",
                "--expand-environment=no",
                "--",
                os.fspath(executable),
                "-m",
                "workers.monomer_dft_worker.app.executor_process",
                "--fd",
                arguments[14],
                "--mode",
                "primary",
                "--model",
                "aimnet2",
                "--gpu-index",
                "1",
            ]
        else:
            assert scope_arguments.exists() is False
    finally:
        executor.close(
            prepare_termination=lambda: pytest.fail(
                "normal graceful shutdown must not call Broker prepare"
            )
        )


def test_executor_spawn_and_preload_share_one_start_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "hung-preload-python"
    executable.write_text(
        f"""#!{sys.executable}
import argparse
import os
import socket
import sys
import time
sys.path.insert(0, {str(Path(__file__).resolve().parents[3])!r})
from workers.monomer_dft_worker.app.executor_ipc import protocol_message, receive_frame, send_frame

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--fd', type=int, required=True)
parser.add_argument('--mode', required=True)
parser.add_argument('--model', required=True)
parser.add_argument('--gpu-index', required=True)
args, _ = parser.parse_known_args()
stream = socket.socket(fileno=args.fd)
gpu_uuid = os.environ['NEXPOLY_DFT_EXECUTOR_GPU_UUID']
send_frame(stream, protocol_message(
    'spawned', pid=os.getpid(), mode=args.mode, model=args.model,
    gpu_index=args.gpu_index, gpu_uuid=gpu_uuid,
    expected_gpu_uuid=gpu_uuid,
    cuda_visible_devices=os.environ['CUDA_VISIBLE_DEVICES'],
))
assert receive_frame(stream)['type'] == 'authorize_cuda'
time.sleep(30)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setattr(executor_pool_module, "EXECUTOR_START_TIMEOUT_SECONDS", 0.25)
    lease = GpuLease(
        lease_id="hung-preload",
        gpu_index="1",
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        kind="residency",
        budget_mib=4096,
        active_thread_percentage=50,
        fencing_token=1,
        preferred=True,
        broker_instance_id="broker-hung-preload",
    )
    executor = SubprocessExecutor(
        settings=dataclasses.replace(_settings(tmp_path), python=executable),
        lease=lease,
        mode="primary",
        model="aimnet2",
    )
    prepared: list[int] = []

    def prepare() -> None:
        assert executor.process is not None
        prepared.append(executor.process.pid)
        executor.process.terminate()

    started = time.monotonic()
    with pytest.raises(GpuRuntimeUnhealthy, match="spawn and preload"):
        executor.start(activate=lambda _pid: None, prepare_termination=prepare)

    assert time.monotonic() - started < 2.0
    assert prepared == [executor.pid]
    assert executor.process is None


def test_forced_executor_close_never_signals_before_mps_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    lease = GpuLease(
        lease_id="termination-lease",
        gpu_index="1",
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        kind="execution",
        budget_mib=4096,
        active_thread_percentage=50,
        fencing_token=1,
        preferred=True,
        broker_instance_id="broker-termination",
    )
    executor = SubprocessExecutor(
        settings=_settings(tmp_path),
        lease=lease,
        mode="overflow",
        model="aimnet2",
    )
    executor.process = process
    executor.pid = process.pid

    def unsafe_prepare() -> None:
        assert process.poll() is None
        raise RuntimeError("MPS termination not proven")

    with pytest.raises(RuntimeError, match="not proven"):
        executor.close(force=True, prepare_termination=unsafe_prepare)
    assert process.poll() is None

    prepared: list[bool] = []
    process_group_signals: list[tuple[int, int]] = []

    monkeypatch.setattr(
        executor_pool_module.os,
        "killpg",
        lambda pid, sig: process_group_signals.append((pid, sig)),
    )

    def safe_prepare() -> None:
        assert process.poll() is None
        prepared.append(True)
        # Simulate the Broker's exact cgroup.kill after its MPS proof.  The
        # Worker must only reap that result and must not signal the process.
        process.kill()

    executor.close(force=True, prepare_termination=safe_prepare)
    assert prepared == [True]
    assert process.poll() is not None
    assert process_group_signals == []


def test_hung_graceful_shutdown_falls_back_to_exact_broker_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class HungProcess:
        pid = 654_322

        def __init__(self) -> None:
            self.prepared = False
            self.exited = False

        def poll(self):
            return -9 if self.exited else None

        def wait(self, timeout=0):
            events.append(("wait", timeout))
            if not self.prepared:
                raise subprocess.TimeoutExpired("executor", timeout)
            self.exited = True
            return -9

    class Stream:
        timeout: float | None = None

        def gettimeout(self):
            return self.timeout

        def settimeout(self, timeout) -> None:
            self.timeout = timeout

        def close(self) -> None:
            events.append(("stream_close", None))

    lease = GpuLease(
        lease_id="hung-graceful",
        gpu_index="1",
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        kind="residency",
        budget_mib=4096,
        active_thread_percentage=50,
        fencing_token=7,
        preferred=True,
        broker_instance_id="broker-hung-graceful",
    )
    executor = SubprocessExecutor(
        settings=_settings(tmp_path), lease=lease, mode="primary", model="aimnet2"
    )
    process = HungProcess()
    executor.process = process  # type: ignore[assignment]
    executor.pid = process.pid
    executor.stream = Stream()  # type: ignore[assignment]

    monkeypatch.setattr(
        executor_pool_module,
        "send_frame",
        lambda _stream, message: events.append(("send", message["type"])),
    )
    monkeypatch.setattr(
        executor_pool_module,
        "receive_frame",
        lambda _stream: (_ for _ in ()).throw(socket.timeout("hung")),
    )
    monkeypatch.setattr(
        executor_pool_module.os,
        "killpg",
        lambda pid, sig: events.append(("signal", (pid, sig))),
    )

    def prepare() -> None:
        events.append(("prepare", lease.lease_id))
        process.prepared = True

    executor.close(force=False, prepare_termination=prepare)

    assert events == [
        ("send", "shutdown"),
        ("prepare", lease.lease_id),
        (
            "wait",
            executor_pool_module.EXECUTOR_BROKER_TERMINATION_EXIT_TIMEOUT_SECONDS,
        ),
        ("stream_close", None),
    ]
    assert process.exited is True
    assert executor.process is None
    assert executor.stream is None


def test_exit_during_broker_prepare_race_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RacingProcess:
        pid = 654_323

        def __init__(self) -> None:
            self.exited = False

        def poll(self):
            return 0 if self.exited else None

        def wait(self, timeout=0):
            raise subprocess.TimeoutExpired("executor", timeout)

    class Stream:
        timeout: float | None = None

        def gettimeout(self):
            return self.timeout

        def settimeout(self, timeout) -> None:
            self.timeout = timeout

        def close(self) -> None:
            raise AssertionError("failed close must retain the suspect IPC handle")

    lease = GpuLease(
        lease_id="prepare-exit-race",
        gpu_index="1",
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        kind="residency",
        budget_mib=4096,
        active_thread_percentage=50,
        fencing_token=9,
        preferred=True,
        broker_instance_id="broker-prepare-exit-race",
    )
    executor = SubprocessExecutor(
        settings=_settings(tmp_path), lease=lease, mode="primary", model="aimnet2"
    )
    process = RacingProcess()
    stream = Stream()
    executor.process = process  # type: ignore[assignment]
    executor.pid = process.pid
    executor.stream = stream  # type: ignore[assignment]
    monkeypatch.setattr(executor_pool_module, "send_frame", lambda *_args: None)
    monkeypatch.setattr(
        executor_pool_module,
        "receive_frame",
        lambda _stream: (_ for _ in ()).throw(socket.timeout("hung")),
    )

    def raced_prepare() -> None:
        process.exited = True
        raise GpuTerminationUnsafe("Broker could not prove the raced workload")

    with pytest.raises(GpuTerminationUnsafe, match="raced workload"):
        executor.close(force=False, prepare_termination=raced_prepare)

    assert process.exited is True
    assert executor.process is process
    assert executor.stream is stream


def test_broker_governed_close_of_naturally_exited_process_is_signal_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class ExitedProcess:
        pid = 654_319

        @staticmethod
        def poll():
            return 0

        @staticmethod
        def wait(timeout=0):
            events.append(("wait", timeout))
            return 0

    class Stream:
        def close(self) -> None:
            events.append(("stream_close", None))

    lease = GpuLease(
        lease_id="natural-exit",
        gpu_index="1",
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        kind="execution",
        budget_mib=4096,
        active_thread_percentage=50,
        fencing_token=1,
        preferred=False,
        broker_instance_id="broker-natural-exit",
        placement="overflow",
    )
    executor = SubprocessExecutor(
        settings=_settings(tmp_path), lease=lease, mode="overflow", model="aimnet2"
    )
    executor.process = ExitedProcess()  # type: ignore[assignment]
    executor.pid = ExitedProcess.pid
    executor.stream = Stream()  # type: ignore[assignment]
    monkeypatch.setattr(
        executor_pool_module,
        "send_frame",
        lambda _stream, message: events.append(("send", message["type"])),
    )
    monkeypatch.setattr(
        executor_pool_module.os,
        "killpg",
        lambda pid, sig: events.append(("signal", (pid, sig))),
    )

    executor.close(
        force=False,
        prepare_termination=lambda: events.append(("prepare", None)),
    )

    assert events == [("wait", 0), ("stream_close", None)]
    assert executor.process is None
    assert executor.stream is None


def test_forced_close_accepts_process_group_gone_after_broker_cgroup_kill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokerKilledProcess:
        pid = 654_321

        def __init__(self) -> None:
            self.prepared = False
            self.reaped = False
            self.waits_after_prepare = 0

        def poll(self):
            return -9 if self.reaped else None

        def wait(self, timeout=0):
            if self.prepared:
                self.waits_after_prepare += 1
                self.reaped = True
                return -9
            raise subprocess.TimeoutExpired("executor", timeout)

    lease = GpuLease(
        lease_id="cgroup-killed",
        gpu_index="1",
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        kind="execution",
        budget_mib=4096,
        active_thread_percentage=50,
        fencing_token=1,
        preferred=False,
        broker_instance_id="broker-cgroup-killed",
        placement="overflow",
    )
    executor = SubprocessExecutor(
        settings=_settings(tmp_path), lease=lease, mode="overflow", model="aimnet2"
    )
    process = BrokerKilledProcess()
    executor.process = process  # type: ignore[assignment]
    executor.pid = process.pid
    signal_attempts: list[int] = []

    def missing_group(_pid: int, sig: int) -> None:
        signal_attempts.append(sig)
        raise ProcessLookupError

    monkeypatch.setattr("workers.monomer_dft_worker.app.executor_pool.os.killpg", missing_group)

    executor.close(
        force=True,
        prepare_termination=lambda: setattr(process, "prepared", True),
    )

    assert signal_attempts == []
    assert process.reaped is True
    assert executor.process is None


def test_user_cancel_waits_for_cooperative_scientific_boundary(
    tmp_path: Path,
) -> None:
    parent, child = socket.socketpair()
    lease = GpuLease(
        lease_id="cancel-lease",
        gpu_index="1",
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        kind="execution",
        budget_mib=4096,
        active_thread_percentage=50,
        fencing_token=1,
        preferred=True,
        broker_instance_id="broker-cancel",
    )
    executor = SubprocessExecutor(
        settings=_settings(tmp_path),
        lease=lease,
        mode="primary",
        model="aimnet2",
    )
    executor.process = SimpleNamespace(poll=lambda: None)
    executor.stream = parent
    identity = {
        "job_id": "executor-job",
        "attempt_token": "a" * 32,
        "request_sha256": _request().request_sha256,
        "enqueue_sequence": 17,
        "lease_id": lease.lease_id,
        "gpu_uuid": lease.gpu_uuid,
        "fencing_token": lease.fencing_token,
    }

    def peer() -> None:
        assert receive_frame(child)["type"] == "execute"
        cancel = receive_frame(child)
        assert cancel["type"] == "cancel"
        time.sleep(0.15)
        send_frame(
            child,
            protocol_message(
                "error",
                identity=identity,
                code="cancelled",
                message="cancelled at a safe boundary",
                retryable=False,
                details={},
                terminate_executor=False,
            ),
        )

    peer_thread = threading.Thread(target=peer)
    peer_thread.start()
    started = time.monotonic()
    with pytest.raises(ComputationCancelled):
        executor.execute(
            _request(),
            tmp_path / "artifacts",
            identity=identity,
            progress=lambda *_args: None,
            cancelled=lambda: True,
            provenance={},
            queue_wait_ms=0.0,
            execution_timings={},
        )
    peer_thread.join(timeout=1.0)
    child.close()
    parent.close()
    assert time.monotonic() - started >= 0.14
    assert executor.broken is False


def test_scientific_result_v2_gpu_provenance_contract_is_strict() -> None:
    valid = {
        "execution_path": "primary",
        "gpu_uuid": "GPU-test",
        "gpu_budget_mib": 4096,
        "broker_instance_id": "broker-test",
        "lease_id": "lease-test",
        "fencing_token": 1,
        "worker_version": "extra-fields-are-preserved",
    }
    parsed = GpuExecutionProvenanceV2.model_validate(valid)
    assert parsed.model_dump()["worker_version"] == "extra-fields-are-preserved"

    for field, invalid in (
        ("execution_path", "gpu3"),
        ("gpu_uuid", ""),
        ("gpu_budget_mib", 0),
        ("broker_instance_id", ""),
        ("lease_id", ""),
        ("fencing_token", 0),
    ):
        payload = dict(valid)
        payload[field] = invalid
        with pytest.raises(Exception):
            GpuExecutionProvenanceV2.model_validate(payload)


def test_broker_disabled_smoke_blocks_docker_declared_gpu3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command, **_kwargs):
        if command[0] == "nvidia-smi":
            return SimpleNamespace(stdout="")
        if command[:3] == ("docker", "ps", "-q"):
            return SimpleNamespace(stdout="container-id\n")
        if command[:2] == ("docker", "inspect"):
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        {
                            "HostConfig": {
                                "DeviceRequests": [
                                    {
                                        "Driver": "nvidia",
                                        "DeviceIDs": ["3"],
                                        "Capabilities": [["gpu"]],
                                    }
                                ]
                            },
                            "Config": {"Env": []},
                        }
                    ]
                )
            )
        raise AssertionError(command)

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert audit_isolated_gpu_availability(("1", "3")) == {"3"}


def test_worker_uses_repository_shared_broker_client_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []

    class SharedError(RuntimeError):
        def __init__(self, code, message):
            super().__init__(message)
            self.code = code

    class Managed:
        def __init__(self):
            self.connectivity_status = "healthy"
            self.lease = SimpleNamespace(
                lease_id="shared-lease",
                fencing_token=7,
                broker_instance_id="shared-broker",
                kind="residency",
                placement="preferred",
                component="dft",
                environment="dev",
                client_id="dft-test",
                gpu_index=1,
                gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
                memory_mib=4096,
                thread_percent=50,
                preferred=True,
                parent_lease_id=None,
                request_id="",
                status="active",
            )

        def assert_healthy(self):
            calls.append(("assert_healthy", None))

        def confirm_current(self):
            calls.append(("confirm_current", None))
            return self.lease

        def register_workload(self, workload_pid):
            calls.append(("register_workload", workload_pid))
            return self.lease

        def prepare_process_termination(self):
            calls.append(("prepare_process_termination", None))
            return {"safe_to_signal": True, "client_pids": [], "prepared_at": 1.0}

        def fail_closed(self):
            calls.append(("fail_closed", None))

        def close(self):
            calls.append(("close", None))

        def quarantine(self, *, reason):
            calls.append(("quarantine", reason))

    managed = Managed()

    class SharedClient:
        def __init__(self, socket_path, *, timeout_seconds):
            calls.append(("init", Path(socket_path)))
            calls.append(("timeout_seconds", timeout_seconds))

        def acquire_managed(self, **kwargs):
            calls.append(("acquire_managed", kwargs))
            managed.lease.request_id = kwargs["request_id"]
            return managed

    module = types.ModuleType("gpu_resource")
    module.GpuBrokerClient = SharedClient
    module.GpuBrokerClientError = SharedError

    def fake_mps_client_environment(lease, *, pipe_root):
        calls.append(("mps_client_environment", (lease.lease_id, Path(pipe_root))))
        return {
            "CUDA_VISIBLE_DEVICES": lease.gpu_uuid,
            "CUDA_MPS_PIPE_DIRECTORY": str(Path(pipe_root) / "mps-1" / "pipe"),
            "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE": "50",
            "CUDA_MPS_CLIENT_PRIORITY": "1",
            "CUDA_MPS_PINNED_DEVICE_MEM_LIMIT": f"{lease.gpu_uuid}=4096M",
        }

    module.mps_client_environment = fake_mps_client_environment
    monkeypatch.setitem(sys.modules, "gpu_resource", module)

    mps_root = tmp_path / "mps-state"
    adapter = SharedGpuBrokerAdapter(
        tmp_path / "broker.sock",
        environment="dev",
        client_id="dft-test",
        mps_pipe_root=mps_root,
        dev_runtime_root=tmp_path,
    )
    lease = adapter.acquire(
        kind="residency",
        gpu_index="1",
        budget_mib=4096,
        active_thread_percentage=50,
        preferred=True,
        placement="preferred",
        parent_lease_id=None,
        owner={},
    )
    acquired = next(value for name, value in calls if name == "acquire_managed")
    request_id = acquired.pop("request_id")
    assert request_id.startswith("dft-")
    assert (
        "timeout_seconds",
        12.0,
    ) in calls
    assert acquired == {
        "kind": "residency",
        "placement": "preferred",
        "component": "dft",
        "environment": "dev",
        "client_id": "dft-test",
        "memory_mib": 4096,
        "thread_percent": 50,
        "wait_timeout_seconds": 0.0,
        "heartbeat_interval_seconds": 5.0,
        "parent_lease_id": None,
    }
    assert ("mps_client_environment", ("shared-lease", mps_root)) in calls
    assert dict(lease.client_environment)["CUDA_VISIBLE_DEVICES"] == lease.gpu_uuid
    assert dict(lease.client_environment)["CUDA_MPS_CLIENT_PRIORITY"] == "1"
    adapter.activate(
        lease,
        pid=os.getpid(),
        process_start_time=process_start_time(os.getpid()),
    )
    adapter.heartbeat(lease)
    assert adapter.prepare_process_termination(lease)["safe_to_signal"] is True
    assert ("register_workload", os.getpid()) in calls
    assert ("confirm_current", None) in calls
    assert ("prepare_process_termination", None) in calls
    adapter.quarantine(lease, reason="cuda_fatal")
    with pytest.raises(GpuLeaseLost, match="PID was reused"):
        adapter.activate(
            lease,
            pid=os.getpid(),
            process_start_time=process_start_time(os.getpid()) + 1,
        )
    adapter.release(lease)
    assert ("quarantine", "gpu_fatal") in calls
    assert ("fail_closed", None) in calls
    assert ("close", None) in calls


def test_shared_broker_adapter_rejects_prod_and_external_runtime_paths(
    tmp_path: Path,
) -> None:
    with pytest.raises(GpuRuntimeUnhealthy, match="production is hard-off"):
        SharedGpuBrokerAdapter(
            tmp_path / "broker.sock",
            environment="prod",
            client_id="dft-test",
            mps_pipe_root=tmp_path / "mps-state",
            dev_runtime_root=tmp_path,
        )
    with pytest.raises(
        GpuRuntimeUnhealthy,
        match="must be located below|production repository",
    ):
        SharedGpuBrokerAdapter(
            Path("/data/lzq/gith/nexpoly/ops/state/gpu-resource/broker.sock"),
            environment="dev",
            client_id="dft-test",
            mps_pipe_root=tmp_path / "mps-state",
            dev_runtime_root=tmp_path,
        )


@pytest.mark.parametrize("loss_code", ("unknown_lease", "stale_fencing_token"))
def test_shared_adapter_heartbeat_synchronously_rejects_lost_fence(
    loss_code: str,
) -> None:
    calls: list[str] = []

    class SharedError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class Managed:
        # Cached health deliberately remains green. Only a synchronous Broker
        # confirmation can observe the authoritative fence loss.
        connectivity_status = "healthy"

        def assert_healthy(self):
            raise AssertionError("heartbeat must not accept cached health")

        def confirm_current(self):
            calls.append("confirm_current")
            raise SharedError(loss_code, "lease is no longer current")

    lease = GpuLease(
        lease_id="execution",
        gpu_index="1",
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        kind="execution",
        budget_mib=4096,
        active_thread_percentage=50,
        fencing_token=2,
        preferred=True,
        broker_instance_id="broker",
        parent_lease_id="resident",
    )
    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._managed = {lease.lease_id: Managed()}
    adapter._lease_contracts = {lease.lease_id: lease}
    adapter._managed_lock = threading.RLock()
    adapter._error_type = SharedError

    with pytest.raises(GpuLeaseLost, match="no longer current"):
        adapter.heartbeat(lease)

    assert calls == ["confirm_current"]


def test_parented_execution_inherits_resident_workload_without_reregistering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Managed:
        connectivity_status = "healthy"

        def assert_healthy(self):
            calls.append("healthy")

        def register_workload(self, _pid):
            raise AssertionError("parented execution must not register workload")

        def fail_closed(self):
            calls.append("fail_closed")

    residency = GpuLease(
        lease_id="resident",
        gpu_index="1",
        gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        kind="residency",
        budget_mib=4096,
        active_thread_percentage=50,
        fencing_token=1,
        preferred=True,
        broker_instance_id="broker",
    )
    execution = dataclasses.replace(
        residency,
        lease_id="execution",
        kind="execution",
        fencing_token=2,
        parent_lease_id=residency.lease_id,
    )
    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._managed = {execution.lease_id: Managed()}
    adapter._lease_contracts = {
        residency.lease_id: residency,
        execution.lease_id: execution,
    }
    adapter._managed_lock = threading.RLock()
    adapter._error_type = RuntimeError
    pid = os.getpid()
    start_time = process_start_time(pid)
    monkeypatch.setattr(
        "workers.monomer_dft_worker.app.gpu_broker_client.read_process_start_time",
        lambda _pid: start_time,
    )

    adapter.activate(execution, pid=pid, process_start_time=start_time)

    assert calls == ["healthy"]


def test_cancelled_shared_waiter_collects_a_raced_lease() -> None:
    acquire_started = threading.Event()
    allow_grant = threading.Event()
    cancellation = threading.Event()
    calls: list[str] = []

    class SharedError(RuntimeError):
        pass

    class Managed:
        def close(self):
            calls.append("close_raced_lease")

        def fail_closed(self):
            calls.append("fail_closed")

    class Client:
        def acquire_managed(self, **_kwargs):
            acquire_started.set()
            assert allow_grant.wait(2.0)
            return Managed()

        def cancel_acquire(self, request_id):
            calls.append(f"cancel:{request_id}")
            allow_grant.set()
            return False

    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()

    def request_cancel() -> None:
        assert acquire_started.wait(2.0)
        cancellation.set()

    cancel_thread = threading.Thread(target=request_cancel)
    cancel_thread.start()
    with pytest.raises(GpuAcquireCancelled):
        adapter._acquire_managed_cancellable(
            {"request_id": "stable-request"},
            request_id="stable-request",
            wait_timeout_seconds=30.0,
            cancelled=cancellation.is_set,
        )
    cancel_thread.join(timeout=2.0)

    assert calls == ["cancel:stable-request", "close_raced_lease"]


def test_uncertain_waiter_cancel_is_collected_before_same_id_can_retry() -> None:
    acquire_started = threading.Event()
    cancel_called = threading.Event()
    allow_grant = threading.Event()
    cancellation = threading.Event()
    calls: list[str] = []
    outcomes: list[BaseException] = []

    class SharedError(RuntimeError):
        pass

    class Managed:
        def close(self):
            calls.append("close_raced_lease")

        def fail_closed(self):
            calls.append("fail_closed")

    class Client:
        def acquire_managed(self, **_kwargs):
            calls.append("acquire")
            acquire_started.set()
            assert allow_grant.wait(3.0)
            return Managed()

        def cancel_acquire(self, request_id):
            calls.append(f"cancel:{request_id}")
            cancel_called.set()
            raise SharedError("response lost")

    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()

    def first_owner() -> None:
        try:
            adapter._acquire_managed_cancellable(
                {"request_id": "stable-request"},
                request_id="stable-request",
                wait_timeout_seconds=30.0,
                cancelled=cancellation.is_set,
            )
        except BaseException as exc:
            outcomes.append(exc)

    owner_thread = threading.Thread(target=first_owner)
    owner_thread.start()
    assert acquire_started.wait(2.0)
    cancellation.set()
    assert cancel_called.wait(2.0)
    assert owner_thread.is_alive()

    with pytest.raises(GpuRuntimeUnhealthy, match="already owned"):
        adapter._acquire_managed_cancellable(
            {"request_id": "stable-request"},
            request_id="stable-request",
            wait_timeout_seconds=30.0,
            cancelled=lambda: False,
        )
    assert calls.count("acquire") == 1

    allow_grant.set()
    owner_thread.join(timeout=3.0)
    assert not owner_thread.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], GpuAcquireCancelled)
    assert calls == ["acquire", "cancel:stable-request", "close_raced_lease"]


def test_lost_acquire_response_recovers_with_the_same_stable_request_id() -> None:
    calls: list[tuple[str, float]] = []

    class SharedError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class Managed:
        pass

    recovered = Managed()

    class Client:
        def acquire_managed(self, **kwargs):
            calls.append((kwargs["request_id"], kwargs["wait_timeout_seconds"]))
            if len(calls) == 1:
                # Model a grant whose UDS response was lost.  The Broker keeps
                # the request-ID-bound lease, so only an exact-ID retry can
                # recover it safely.
                raise SharedError("gpu_broker_unavailable", "response lost")
            return recovered

        def cancel_acquire(self, _request_id):
            raise AssertionError("an uncancelled recovery must not cancel")

    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()

    managed = adapter._acquire_managed_cancellable(
        {"request_id": "stable-request", "wait_timeout_seconds": 10.0},
        request_id="stable-request",
        wait_timeout_seconds=10.0,
        cancelled=lambda: False,
    )

    assert managed is recovered
    assert [request_id for request_id, _timeout in calls] == [
        "stable-request",
        "stable-request",
    ]
    assert 0.0 <= calls[1][1] <= calls[0][1] <= 10.0


@pytest.mark.parametrize(
    "error_code", ("internal_error", "unsafe_state", "invalid_parent_lease")
)
def test_post_allocation_broker_error_recovers_with_stable_request_id(
    error_code: str,
) -> None:
    calls: list[str] = []

    class SharedError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class Managed:
        pass

    recovered = Managed()

    class Client:
        def acquire_managed(self, **kwargs):
            calls.append(kwargs["request_id"])
            if len(calls) == 1:
                # HostGpuBroker can create the in-memory lease and then fail
                # _persist_locked(). The server exposes raw OSError as
                # internal_error and an unsafe state path as unsafe_state.
                # Activation can likewise reject a parent only after the child
                # lease was allocated, so invalid_parent_lease is ambiguous at
                # this adapter boundary too.
                raise SharedError(error_code, "post-allocation persistence failed")
            return recovered

        def cancel_acquire(self, _request_id):
            raise AssertionError("uncancelled exact-ID recovery must not cancel")

    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()
    adapter._admission_uncertain = False

    managed = adapter._acquire_managed_cancellable(
        {"request_id": "stable-request", "wait_timeout_seconds": 10.0},
        request_id="stable-request",
        wait_timeout_seconds=10.0,
        cancelled=lambda: False,
    )

    assert managed is recovered
    assert calls == ["stable-request", "stable-request"]
    assert adapter.admission_uncertain is False


def test_cancelled_lost_acquire_response_recovers_and_closes_exact_lease() -> None:
    calls: list[tuple[str, float]] = []
    cancellation = threading.Event()
    closed: list[str] = []

    class SharedError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class Managed:
        def close(self):
            closed.append("exact-recovered-lease")

        def fail_closed(self):
            closed.append("fail-closed")

    class Client:
        def acquire_managed(self, **kwargs):
            calls.append((kwargs["request_id"], kwargs["wait_timeout_seconds"]))
            if len(calls) == 1:
                cancellation.set()
                raise SharedError("invalid_response", "truncated grant response")
            return Managed()

        def cancel_acquire(self, request_id):
            assert request_id == "stable-request"
            return False

    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()

    with pytest.raises(GpuAcquireCancelled):
        adapter._acquire_managed_cancellable(
            {"request_id": "stable-request", "wait_timeout_seconds": 10.0},
            request_id="stable-request",
            wait_timeout_seconds=10.0,
            cancelled=cancellation.is_set,
        )

    assert [request_id for request_id, _timeout in calls] == [
        "stable-request",
        "stable-request",
    ]
    assert calls[1][1] == 0.0
    assert closed == ["exact-recovered-lease"]


def test_cancellation_after_waiter_stores_grant_closes_exact_lease() -> None:
    callback_calls = 0
    closed: list[str] = []

    class SharedError(RuntimeError):
        pass

    class Managed:
        def close(self):
            closed.append("late-grant")

        def fail_closed(self):
            closed.append("fail-closed")

    class Client:
        def acquire_managed(self, **_kwargs):
            return Managed()

        def cancel_acquire(self, _request_id):
            raise AssertionError("the grant completed before the outer poll")

    def cancelled() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        # The waiter observes false before the grant; the owning thread observes
        # true immediately after ``finished``.  This deterministically exercises
        # the late-cancellation window without scheduler sleeps.
        return callback_calls >= 2

    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()

    with pytest.raises(GpuAcquireCancelled):
        adapter._acquire_managed_cancellable(
            {"request_id": "stable-request", "wait_timeout_seconds": 10.0},
            request_id="stable-request",
            wait_timeout_seconds=10.0,
            cancelled=cancelled,
        )

    assert callback_calls == 2
    assert closed == ["late-grant"]


@pytest.mark.parametrize(
    "ambiguous_error_code",
    ("invalid_response", "internal_error", "unsafe_state"),
)
def test_ambiguous_first_generation_after_authoritative_cancel_is_uncertain(
    ambiguous_error_code: str,
) -> None:
    acquire_started = threading.Event()
    cancel_linearized = threading.Event()
    cancellation = threading.Event()
    acquire_calls: list[float] = []

    class SharedError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class Client:
        timeout_seconds = 0.0

        def acquire_managed(self, **kwargs):
            acquire_calls.append(kwargs["wait_timeout_seconds"])
            if len(acquire_calls) != 1:
                raise AssertionError(
                    "an authoritative cancel must prevent post-cancel reacquire"
                )
            # This first local generation has crossed the decision gate but has
            # not reached the Broker yet. The Broker may already hold the same
            # stable identity from an earlier ambiguous transport attempt.
            acquire_started.set()
            assert cancel_linearized.wait(2.0)
            # After cancel=True removed that existing waiter, this delayed send
            # can recreate the identity and lose its response.
            raise SharedError(
                ambiguous_error_code,
                "post-cancel response did not prove ownership",
            )

        def cancel_acquire(self, request_id):
            assert request_id == "stable-request"
            cancel_linearized.set()
            # The positive result belongs to the same-ID waiter that existed
            # before this local generation reached the Broker.
            time.sleep(0.05)
            return True

    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()
    adapter._admission_uncertain = False

    def request_cancel() -> None:
        assert acquire_started.wait(2.0)
        cancellation.set()

    cancel_thread = threading.Thread(target=request_cancel)
    cancel_thread.start()
    with pytest.raises(GpuRuntimeUnhealthy, match="ownership is unresolved"):
        adapter._acquire_managed_cancellable(
            {"request_id": "stable-request", "wait_timeout_seconds": 10.0},
            request_id="stable-request",
            wait_timeout_seconds=10.0,
            cancelled=cancellation.is_set,
        )
    cancel_thread.join(timeout=2.0)

    assert acquire_calls == [10.0]
    assert adapter.admission_uncertain is True


def test_authoritative_cancel_with_explicit_acquire_terminal_is_safe() -> None:
    acquire_started = threading.Event()
    cancel_linearized = threading.Event()
    cancellation = threading.Event()

    class SharedError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class Client:
        timeout_seconds = 0.0

        def acquire_managed(self, **_kwargs):
            acquire_started.set()
            assert cancel_linearized.wait(2.0)
            raise SharedError("acquire_cancelled", "waiter was removed")

        def cancel_acquire(self, request_id):
            assert request_id == "stable-request"
            cancel_linearized.set()
            return True

    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()
    adapter._admission_uncertain = False

    def request_cancel() -> None:
        assert acquire_started.wait(2.0)
        cancellation.set()

    cancel_thread = threading.Thread(target=request_cancel)
    cancel_thread.start()
    with pytest.raises(GpuAcquireCancelled, match="cancelled"):
        adapter._acquire_managed_cancellable(
            {"request_id": "stable-request", "wait_timeout_seconds": 10.0},
            request_id="stable-request",
            wait_timeout_seconds=10.0,
            cancelled=cancellation.is_set,
        )
    cancel_thread.join(timeout=2.0)

    assert adapter.admission_uncertain is False


def test_ambiguous_inflight_recovery_after_authoritative_cancel_is_uncertain() -> None:
    recovery_entered = threading.Event()
    cancel_linearized = threading.Event()
    cancellation = threading.Event()
    post_cancel_request_created = threading.Event()
    acquire_calls: list[str] = []

    class SharedError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class Client:
        timeout_seconds = 0.0

        def acquire_managed(self, **kwargs):
            acquire_calls.append(kwargs["request_id"])
            if len(acquire_calls) == 1:
                # The first response is lost while its old Broker waiter remains.
                raise SharedError("invalid_response", "old waiter response lost")
            if len(acquire_calls) == 2:
                # This recovery has crossed the local decision check, but its
                # simulated UDS send is held until cancel=True removes the old
                # waiter. It then creates a post-cancel identity and loses that
                # response too.
                recovery_entered.set()
                assert cancel_linearized.wait(2.0)
                post_cancel_request_created.set()
                raise SharedError("invalid_response", "post-cancel grant response lost")
            raise AssertionError("unsafe recovery must stop after generation two")

        def cancel_acquire(self, request_id):
            assert request_id == "stable-request"
            cancel_linearized.set()
            return True

    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()
    adapter._admission_uncertain = False

    def request_cancel() -> None:
        assert recovery_entered.wait(2.0)
        cancellation.set()

    cancel_thread = threading.Thread(target=request_cancel)
    cancel_thread.start()
    with pytest.raises(GpuRuntimeUnhealthy, match="ownership is unresolved"):
        adapter._acquire_managed_cancellable(
            {"request_id": "stable-request", "wait_timeout_seconds": 10.0},
            request_id="stable-request",
            wait_timeout_seconds=10.0,
            cancelled=cancellation.is_set,
        )
    cancel_thread.join(timeout=2.0)

    assert post_cancel_request_created.is_set()
    assert acquire_calls == ["stable-request", "stable-request"]
    assert adapter.admission_uncertain is True


def test_unreachable_broker_bounds_cancel_and_blocks_future_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = threading.Event()
    stop_recovery = threading.Event()
    acquire_calls = 0

    class SharedError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class Client:
        timeout_seconds = 0.0

        def acquire_managed(self, **_kwargs):
            nonlocal acquire_calls
            acquire_calls += 1
            cancellation.set()
            if stop_recovery.is_set():
                raise SharedError("acquire_cancelled", "test cleanup")
            raise SharedError("gpu_broker_unavailable", "offline")

        def cancel_acquire(self, _request_id):
            raise SharedError("gpu_broker_unavailable", "offline")

    monkeypatch.setattr(
        gpu_broker_client_module,
        "STABLE_ACQUIRE_COLLECTION_GRACE_SECONDS",
        0.05,
    )
    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()
    adapter._admission_uncertain = False
    adapter.environment = "dev"
    adapter.client_id = "dft-test"

    started = time.monotonic()
    try:
        with pytest.raises(GpuAcquireCancelled, match="restart required"):
            adapter._acquire_managed_cancellable(
                {"request_id": "stable-request", "wait_timeout_seconds": 30.0},
                request_id="stable-request",
                wait_timeout_seconds=30.0,
                cancelled=cancellation.is_set,
            )
        assert time.monotonic() - started < 0.5
        assert adapter.admission_uncertain is True
        prior_calls = acquire_calls
        with pytest.raises(GpuRuntimeUnhealthy, match="unresolved ownership"):
            adapter.acquire(
                kind="execution",
                gpu_index="3",
                budget_mib=4096,
                active_thread_percentage=50,
                preferred=False,
                placement="overflow",
                parent_lease_id=None,
                owner={"request_id": "later-request"},
            )
        assert acquire_calls == prior_calls
    finally:
        stop_recovery.set()


def test_detached_admission_owner_fail_closes_late_grant_after_caller_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = threading.Event()
    allow_late_grant = threading.Event()
    close_attempted = threading.Event()
    fail_closed = threading.Event()
    acquire_calls = 0

    class SharedError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class Managed:
        def close(self):
            close_attempted.set()
            raise RuntimeError("release response was lost")

        def fail_closed(self):
            fail_closed.set()

    class Client:
        timeout_seconds = 0.0

        def acquire_managed(self, **_kwargs):
            nonlocal acquire_calls
            acquire_calls += 1
            if acquire_calls == 1:
                cancellation.set()
                raise SharedError("gpu_broker_unavailable", "grant response was lost")
            assert allow_late_grant.wait(2.0)
            return Managed()

        def cancel_acquire(self, _request_id):
            raise SharedError("gpu_broker_unavailable", "cancel response was lost")

    monkeypatch.setattr(
        gpu_broker_client_module,
        "STABLE_ACQUIRE_COLLECTION_GRACE_SECONDS",
        0.05,
    )
    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()
    adapter._admission_uncertain = False

    with pytest.raises(GpuAcquireCancelled, match="restart required"):
        adapter._acquire_managed_cancellable(
            {"request_id": "stable-request", "wait_timeout_seconds": 30.0},
            request_id="stable-request",
            wait_timeout_seconds=30.0,
            cancelled=cancellation.is_set,
        )
    assert adapter.admission_uncertain is True
    assert close_attempted.is_set() is False

    allow_late_grant.set()
    assert close_attempted.wait(2.0)
    assert fail_closed.wait(2.0)
    assert acquire_calls == 2
    assert adapter.admission_uncertain is True


def test_transport_deadline_bounds_uncancelled_unreachable_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_recovery = threading.Event()

    class SharedError(RuntimeError):
        def __init__(self, code: str, message: str) -> None:
            super().__init__(message)
            self.code = code

    class Client:
        timeout_seconds = 0.0

        def acquire_managed(self, **_kwargs):
            if stop_recovery.is_set():
                raise SharedError("acquire_cancelled", "test cleanup")
            raise SharedError("gpu_broker_unavailable", "offline")

        def cancel_acquire(self, _request_id):
            raise SharedError("gpu_broker_unavailable", "offline")

    monkeypatch.setattr(
        gpu_broker_client_module,
        "STABLE_ACQUIRE_COLLECTION_GRACE_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        gpu_broker_client_module,
        "STABLE_ACQUIRE_SCHEDULING_ALLOWANCE_SECONDS",
        0.0,
    )
    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()
    adapter._admission_uncertain = False

    started = time.monotonic()
    try:
        with pytest.raises(GpuRuntimeUnhealthy, match="bounded transport deadline"):
            adapter._acquire_managed_cancellable(
                {"request_id": "stable-request", "wait_timeout_seconds": 0.0},
                request_id="stable-request",
                wait_timeout_seconds=0.0,
                cancelled=lambda: False,
            )
        assert time.monotonic() - started < 0.5
        assert adapter.admission_uncertain is True
    finally:
        stop_recovery.set()


def test_repeated_cancel_signal_closes_raced_grant_exactly_once() -> None:
    acquire_started = threading.Event()
    cancel_entered = threading.Event()
    allow_cancel_return = threading.Event()
    cancellation = threading.Event()
    calls: list[str] = []

    class SharedError(RuntimeError):
        pass

    class Managed:
        def close(self):
            calls.append("close")

        def fail_closed(self):
            calls.append("fail_closed")

    class Client:
        timeout_seconds = 0.0

        def acquire_managed(self, **_kwargs):
            acquire_started.set()
            assert cancel_entered.wait(2.0)
            return Managed()

        def cancel_acquire(self, request_id):
            calls.append(f"cancel:{request_id}")
            cancel_entered.set()
            assert allow_cancel_return.wait(2.0)
            return False

    adapter = object.__new__(SharedGpuBrokerAdapter)
    adapter._client = Client()
    adapter._error_type = SharedError
    adapter._inflight_acquire_lock = threading.Lock()
    adapter._inflight_acquire_request_ids = set()
    adapter._admission_uncertain = False

    def request_cancel_twice() -> None:
        assert acquire_started.wait(2.0)
        cancellation.set()
        assert cancel_entered.wait(2.0)
        cancellation.set()
        allow_cancel_return.set()

    cancel_thread = threading.Thread(target=request_cancel_twice)
    cancel_thread.start()
    with pytest.raises(GpuAcquireCancelled):
        adapter._acquire_managed_cancellable(
            {"request_id": "stable-request", "wait_timeout_seconds": 30.0},
            request_id="stable-request",
            wait_timeout_seconds=30.0,
            cancelled=cancellation.is_set,
        )
    cancel_thread.join(timeout=2.0)

    assert calls == ["cancel:stable-request", "close"]
    assert adapter.admission_uncertain is False


def test_primary_executor_is_resident_and_execution_is_fenced(tmp_path: Path) -> None:
    broker = ScriptedBroker()
    factory = HandleFactory()
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)
    pool.start()

    execution = pool.execute(
        _request(),
        tmp_path / "output",
        progress=lambda *_args: None,
        cancelled=lambda: False,
        provenance={"worker_version": "test"},
        queue_wait_ms=4.0,
    )

    execute_call = next(item for item in factory.calls if item[0] == "execute")
    identity = execute_call[3]
    assert execute_call[1:3] == ("primary", "1")
    assert identity == {
        "job_id": "executor-job",
        "attempt_token": "a" * 32,
        "request_sha256": _request().request_sha256,
        "enqueue_sequence": 17,
        "lease_id": identity["lease_id"],
        "gpu_uuid": "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
        "fencing_token": identity["fencing_token"],
    }
    assert execution.result["provenance"]["execution_path"] == "primary"
    assert execution.result["provenance"]["gpu_budget_mib"] == 4096
    assert execution.result["provenance"]["broker_instance_id"].startswith("disabled-")
    assert execution.result["provenance"]["lease_id"] == identity["lease_id"]
    assert execution.result["provenance"]["fencing_token"] == identity["fencing_token"]
    assert not {
        "gpu_lease_id",
        "gpu_fencing_token",
        "gpu_broker_instance_id",
    } & execution.result["provenance"].keys()
    assert execution.timings["gpu_wait_ms"] >= 0
    assert execution.timings["model_load_ms"] == 0
    assert len(factory.handles) == 1
    pool.close()


def test_unresolved_admission_degrades_pool_readiness_without_reusing_primary(
    tmp_path: Path,
) -> None:
    broker = ScriptedBroker()
    factory = HandleFactory()
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)
    pool.start()
    primary = pool._primary
    broker.admission_uncertain = True

    probe = pool.probe()

    assert probe.ready is False
    assert probe.model_loaded is False
    assert "admission ownership is unresolved" in str(probe.error)
    assert pool._primary is primary
    pool.close()


def test_authoritative_admission_timing_is_rewritten_and_rechecksummed(
    tmp_path: Path,
) -> None:
    broker = ScriptedBroker()
    factory = HandleFactory()
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)
    pool.start()

    execution = pool.execute(
        _request(),
        tmp_path / "output",
        admitted=lambda: 432.5,
        progress=lambda *_args: None,
        cancelled=lambda: False,
        provenance={"worker_version": "test"},
        queue_wait_ms=4.0,
    )

    descriptor, path = next(
        item
        for item in execution.artifacts
        if item[0].artifact_id == "scientific_result"
    )
    artifact_result = json.loads(path.read_text(encoding="utf-8"))
    assert execution.timings["gpu_wait_ms"] == 432.5
    assert artifact_result == execution.result
    assert artifact_result["timings"] == execution.timings
    assert descriptor.sha256 == sha256_file(path)
    assert descriptor.size_bytes == path.stat().st_size
    pool.close()


def test_dev_falls_back_only_to_gpu3_and_loads_requested_model(tmp_path: Path) -> None:
    broker = ScriptedBroker(blocked_execution={"1"})
    factory = HandleFactory()
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)
    pool.start()

    result = pool.execute(
        _request("aimnet2-nse"),
        tmp_path / "output",
        progress=lambda *_args: None,
        cancelled=lambda: False,
        provenance={},
        queue_wait_ms=0,
    )

    assert [(handle.mode, handle.lease.gpu_index, handle.model) for handle in factory.handles] == [
        ("primary", "1", "aimnet2"),
        ("overflow", "3", "aimnet2-nse"),
    ]
    assert result.result["provenance"]["execution_path"] == "overflow"
    assert result.result["provenance"]["gpu_physical_device"] == "3"
    assert result.result["provenance"]["physical_gpu"] == "3"
    assert result.result["provenance"]["visible_gpu_count"] == 1
    assert result.result["provenance"]["gpu_name"] == "Fake GPU 3"
    assert result.result["provenance"]["model_alias"] == "aimnet2-nse"
    assert result.result["provenance"]["model_registry_key"] == (
        "aimnet2-nse-registry"
    )
    assert result.timings["model_load_ms"] == 9.0
    assert not any(item[1] == "2" for item in broker.acquires)
    pool.close()


def test_shared_broker_owns_overflow_order_with_one_stable_waiter(
    tmp_path: Path,
) -> None:
    class ManagedPlacementBroker(ScriptedBroker):
        managed_placement = True

        def __init__(self):
            super().__init__(blocked_execution={"1"})
            self.managed_placement = True
            self.overflow_requests: list[dict[str, Any]] = []

        def acquire(self, **kwargs):
            if kwargs["placement"] == "overflow":
                self.overflow_requests.append(dict(kwargs))
            return super().acquire(**kwargs)

    broker = ManagedPlacementBroker()
    factory = HandleFactory()
    pool = ExecutorPool(
        _settings(tmp_path),
        broker=broker,
        process_factory=factory,
    )
    pool.start()

    result = pool.execute(
        _request(),
        tmp_path / "output",
        progress=lambda *_args: None,
        cancelled=lambda: False,
        provenance={},
        queue_wait_ms=0.0,
    )

    assert len(broker.overflow_requests) == 1
    request = broker.overflow_requests[0]
    assert request["gpu_index"] == "3"
    assert request["wait_timeout_seconds"] == 600.0
    assert request["owner"]["request_id"].startswith("dft-")
    assert result.result["provenance"]["gpu_physical_device"] == "3"
    pool.close()


def test_result_is_discarded_when_final_lease_fence_is_lost(tmp_path: Path) -> None:
    broker = ScriptedBroker(lose_on_heartbeat=2)
    factory = HandleFactory()
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)
    pool.start()

    with pytest.raises(ScientificComputationError) as caught:
        pool.execute(
            _request(),
            tmp_path / "output",
            progress=lambda *_args: None,
            cancelled=lambda: False,
            provenance={},
            queue_wait_ms=0,
        )

    assert caught.value.code == "gpu_lease_lost"
    assert len([item for item in factory.calls if item[0] == "execute"]) == 1
    assert pool.probe().ready is False


@pytest.mark.parametrize("code,quarantined", (("gpu_oom", False), ("cuda_fatal", True)))
def test_oom_or_fatal_destroys_primary_without_retrying_attempt(
    tmp_path: Path,
    code: str,
    quarantined: bool,
) -> None:
    broker = ScriptedBroker()
    factory = HandleFactory(
        ScientificComputationError(code, "injected", retryable=True)
    )
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)
    pool.start()
    with pytest.raises(ScientificComputationError) as caught:
        pool.execute(
            _request(),
            tmp_path / "output",
            progress=lambda *_args: None,
            cancelled=lambda: False,
            provenance={},
            queue_wait_ms=0,
        )
    assert caught.value.code == code
    assert len([item for item in factory.calls if item[0] == "execute"]) == 1
    assert pool.probe().ready is (code == "gpu_oom")
    assert len(factory.handles) == (2 if code == "gpu_oom" else 1)
    assert ("1" in broker.quarantines) is quarantined
    residency = next(
        lease for lease in broker.acquired_leases if lease.kind == "residency"
    )
    execution = next(
        lease
        for lease in broker.acquired_leases
        if lease.kind == "execution" and lease.parent_lease_id is not None
    )
    assert broker.termination_prepares == [residency.lease_id]
    assert execution.lease_id not in broker.termination_prepares
    assert pool.fatal_restart_safe() is quarantined


def test_overflow_failure_uses_unparented_execution_termination_authority(
    tmp_path: Path,
) -> None:
    broker = ScriptedBroker(blocked_execution={"1"})
    factory = HandleFactory(
        ScientificComputationError("gpu_oom", "injected", retryable=True)
    )
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)
    pool.start()

    with pytest.raises(ScientificComputationError) as caught:
        pool.execute(
            _request(),
            tmp_path / "output",
            progress=lambda *_args: None,
            cancelled=lambda: False,
            provenance={},
            queue_wait_ms=0,
        )

    assert caught.value.code == "gpu_oom"
    overflow = next(
        lease
        for lease in broker.acquired_leases
        if lease.kind == "execution" and lease.parent_lease_id is None
    )
    assert broker.termination_prepares == [overflow.lease_id]
    pool.close()


def test_timeout_and_execute_thread_have_one_cleanup_owner_and_rebuild_primary(
    tmp_path: Path,
) -> None:
    class StrictReleaseBroker(ScriptedBroker):
        def release(self, lease):
            if lease.lease_id in self.releases:
                raise RuntimeError("duplicate lease release")
            return super().release(lease)

    started = threading.Event()
    forced_closed = threading.Event()

    class BlockingHandle(FakeHandle):
        def execute(self, *_args, **_kwargs):
            started.set()
            assert forced_closed.wait(3.0)
            self.broken = True
            raise EOFError("executor was terminated")

        def close(self, *, force=False, prepare_termination=None) -> None:
            self.closed += 1
            self.calls.append(("close", self.mode, force))
            if force and prepare_termination is not None:
                prepare_termination()
            forced_closed.set()

    class BlockingThenHealthyFactory(HandleFactory):
        def __call__(self, **kwargs):
            handle = (
                BlockingHandle(calls=self.calls, **kwargs)
                if not self.handles
                else FakeHandle(calls=self.calls, **kwargs)
            )
            self.handles.append(handle)
            return handle

    broker = StrictReleaseBroker()
    factory = BlockingThenHealthyFactory()
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)
    pool.start()
    failures: list[BaseException] = []

    def execute() -> None:
        try:
            pool.execute(
                _request(),
                tmp_path / "output",
                admitted=lambda: 1.0,
                progress=lambda *_args: None,
                cancelled=lambda: False,
                provenance={},
                queue_wait_ms=0.0,
            )
        except BaseException as exc:
            failures.append(exc)

    execution_thread = threading.Thread(target=execute)
    execution_thread.start()
    assert started.wait(2.0)
    assert pool.terminate_active("timeout") is True
    execution_thread.join(timeout=3.0)

    assert not execution_thread.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], ScientificComputationError)
    assert len(broker.releases) == len(set(broker.releases))
    assert factory.handles[0].closed == 1
    assert len(factory.handles) == 2
    assert pool.probe().ready is True
    pool.close()
    assert len(broker.releases) == len(set(broker.releases))


def test_primary_rebuild_attempts_are_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingFactory(HandleFactory):
        def __call__(self, **kwargs):
            handle = FakeHandle(
                calls=self.calls,
                start_failure=RuntimeError("rebuild failed"),
                **kwargs,
            )
            self.handles.append(handle)
            return handle

    monkeypatch.setattr(executor_pool_module, "PRIMARY_REBUILD_BACKOFF_SECONDS", 0.0)
    broker = ScriptedBroker()
    factory = FailingFactory()
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)

    assert pool._rebuild_primary_bounded() is False
    assert len(factory.handles) == 3
    assert len(broker.releases) == 3
    assert len(set(broker.releases)) == 3
    assert pool.probe().ready is False


def test_fatal_cleanup_prepare_failure_detaches_primary_and_never_releases_suspect(
    tmp_path: Path,
) -> None:
    broker = ScriptedBroker(fail_prepare=True)
    factory = HandleFactory(
        ScientificComputationError("cuda_fatal", "injected", retryable=True)
    )
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)
    pool.start()

    with pytest.raises(ScientificComputationError) as caught:
        pool.execute(
            _request(),
            tmp_path / "output",
            admitted=lambda: None,
            progress=lambda *_args: None,
            cancelled=lambda: False,
            provenance={},
            queue_wait_ms=0,
        )

    assert caught.value.code == "cuda_fatal"
    assert pool.probe().ready is False
    assert pool._primary is None
    assert pool._fatal is True
    execution_lease = next(
        lease for lease in broker.acquired_leases if lease.kind == "execution"
    )
    residency_lease = next(
        lease for lease in broker.acquired_leases if lease.kind == "residency"
    )
    assert execution_lease.lease_id in broker.abandons
    assert residency_lease.lease_id in broker.abandons
    assert execution_lease.lease_id not in broker.releases
    assert residency_lease.lease_id not in broker.releases
    assert pool.fatal_restart_safe() is False


def test_overflow_start_cleanup_failure_is_fatal_and_keeps_managed_lease_suspect(
    tmp_path: Path,
) -> None:
    broker = ScriptedBroker(blocked_execution={"1"}, fail_prepare=True)
    factory = HandleFactory(
        overflow_start_failure=RuntimeError("overflow startup failed")
    )
    pool = ExecutorPool(_settings(tmp_path), broker=broker, process_factory=factory)
    pool.start()

    with pytest.raises(ScientificComputationError) as caught:
        pool.execute(
            _request(),
            tmp_path / "output",
            admitted=lambda: None,
            progress=lambda *_args: None,
            cancelled=lambda: False,
            provenance={},
            queue_wait_ms=0,
        )

    assert caught.value.code == "cuda_fatal"
    assert pool.probe().ready is False
    assert pool._fatal is True
    overflow_lease = next(
        lease
        for lease in broker.acquired_leases
        if lease.kind == "execution" and lease.gpu_index == "3"
    )
    assert overflow_lease.lease_id in broker.abandons
    assert overflow_lease.lease_id not in broker.releases


class _AliveProcess:
    def poll(self):
        return None


def test_subprocess_executor_rejects_late_or_misfenced_response(tmp_path: Path) -> None:
    broker = ScriptedBroker()
    lease = broker.acquire(
        kind="execution",
        gpu_index="1",
        budget_mib=0,
        active_thread_percentage=50,
        preferred=True,
        placement="preferred",
        parent_lease_id=None,
        owner={},
    )
    executor = SubprocessExecutor(
        settings=_settings(tmp_path), lease=lease, mode="primary", model="aimnet2"
    )
    parent, child = socket.socketpair()
    executor.stream = parent
    executor.process = _AliveProcess()  # type: ignore[assignment]

    def peer() -> None:
        command = receive_frame(child)
        wrong = dict(command["identity"])
        wrong["fencing_token"] += 1
        send_frame(
            child,
            protocol_message(
                "result",
                identity=wrong,
                result={},
                timings={},
                artifacts=[],
            ),
        )

    thread = threading.Thread(target=peer)
    thread.start()
    identity = {
        "job_id": "executor-job",
        "attempt_token": "a" * 32,
        "request_sha256": _request().request_sha256,
        "enqueue_sequence": 17,
        "lease_id": lease.lease_id,
        "gpu_uuid": lease.gpu_uuid,
        "fencing_token": lease.fencing_token,
    }
    with pytest.raises(ExecutorProtocolError, match="fenced"):
        executor.execute(
            _request(),
            tmp_path / "output",
            identity=identity,
            progress=lambda *_args: None,
            cancelled=lambda: False,
            provenance={},
            queue_wait_ms=0,
            execution_timings={"gpu_wait_ms": 0.0, "model_load_ms": 0.0},
        )
    thread.join(timeout=2)
    parent.close()
    child.close()
