"""CPU-only supervisor facade for resident and overflow GPU executors."""

from __future__ import annotations

import contextlib
import hashlib
import os
import select
import signal
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from gpu_resource import transient_scope_command

from .artifacts import atomic_write_json, describe_artifact
from .config import REPO_ROOT, WorkerSettings
from .engine import ComputationCancelled, EngineExecution, ScientificComputationError
from .executor_ipc import (
    ExecutorProtocolError,
    protocol_message,
    receive_frame,
    send_frame,
    validate_message,
)
from .gpu_broker_client import (
    DisabledBrokerClient,
    GpuAcquireCancelled,
    GpuBrokerClient,
    GpuBrokerError,
    GpuCapacityUnavailable,
    GpuLease,
    GpuLeaseLost,
    GpuRuntimeUnhealthy,
    GpuTerminationUnsafe,
    SharedGpuBrokerAdapter,
    audit_isolated_gpu_availability,
    process_start_time,
    verify_host_gpu_inventory,
)
from .schemas import ArtifactDescriptor, JobSubmitRequest


EXECUTOR_START_TIMEOUT_SECONDS = 60.0
LEASE_HEARTBEAT_SECONDS = 1.0
PRIMARY_REBUILD_ATTEMPTS = 3
PRIMARY_REBUILD_BACKOFF_SECONDS = 0.1


@dataclass(slots=True)
class _AttemptCleanupState:
    finished: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


@dataclass(frozen=True, slots=True)
class SupervisorRuntimeProbe:
    ready: bool
    model_loaded: bool
    model_name: str
    model_file: str = ""
    model_sha256: str | None = None
    aimnet_origin: str | None = None
    torch_version: str | None = None
    cuda_runtime: str | None = None
    gpu_name: str | None = None
    visible_gpu_count: int = 0
    logical_device: str = "cuda:0"
    loaded_at_unix: float | None = None
    error: str | None = None
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    aimnet_version: str | None = None
    aimnet_commit: str | None = None
    aimnet_wheel_sha256: str | None = None
    warp_version: str | None = None
    gpu_uuid: str | None = None
    execution_path: str = "primary"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExecutorHandle(Protocol):
    pid: int
    model_load_ms: float
    probe_payload: dict[str, Any]
    broken: bool

    def start(
        self,
        activate: Callable[[int], None] | None = None,
        prepare_termination: Callable[[], None] | None = None,
    ) -> None: ...

    def execute(
        self,
        request: JobSubmitRequest,
        output_directory: Path,
        *,
        identity: dict[str, Any],
        progress: Callable[[str, int, str | None], None],
        cancelled: Callable[[], bool],
        provenance: dict[str, Any],
        queue_wait_ms: float,
        execution_timings: dict[str, float],
    ) -> EngineExecution: ...

    def close(
        self,
        *,
        force: bool = False,
        prepare_termination: Callable[[], None] | None = None,
    ) -> None: ...


class SubprocessExecutor:
    def __init__(
        self,
        *,
        settings: WorkerSettings,
        lease: GpuLease,
        mode: str,
        model: str,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        scope_command_builder: Callable[
            [object, list[str]], tuple[str, ...]
        ] = transient_scope_command,
    ) -> None:
        self.settings = settings
        self.lease = lease
        self.mode = mode
        self.model = model
        self._popen = popen
        self._scope_command_builder = scope_command_builder
        self.process: subprocess.Popen[bytes] | None = None
        self.stream: socket.socket | None = None
        self.pid = 0
        self.model_load_ms = 0.0
        self.probe_payload: dict[str, Any] = {}
        self.broken = False
        self._close_lock = threading.Lock()

    def start(
        self,
        activate: Callable[[int], None] | None = None,
        prepare_termination: Callable[[], None] | None = None,
    ) -> None:
        if self.process is not None:
            return
        start_deadline = time.monotonic() + EXECUTOR_START_TIMEOUT_SECONDS
        parent_stream, child_stream = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.update(
            {
                "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
                "MONOMER_DFT_EXECUTOR_PROCESS": "1",
                "NEXPOLY_DFT_EXECUTOR_GPU_DEVICE": self.lease.gpu_index,
                "NEXPOLY_DFT_EXECUTOR_GPU_UUID": self.lease.gpu_uuid,
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        if self.lease.client_environment:
            env.update(dict(self.lease.client_environment))
        else:
            # Broker-disabled development smoke has no MPS server.
            env["CUDA_VISIBLE_DEVICES"] = self.lease.gpu_index
        command = [
            os.fspath(self.settings.python),
            "-m",
            "workers.monomer_dft_worker.app.executor_process",
            "--fd",
            str(child_stream.fileno()),
            "--mode",
            self.mode,
            "--model",
            self.model,
            "--gpu-index",
            self.lease.gpu_index,
        ]
        launch_command: list[str] | tuple[str, ...] = command
        if self.lease.client_environment:
            # systemd-run --user --scope execs the target synchronously: the
            # Popen PID, inherited socket FD and start_new_session PGID remain
            # stable while systemd moves that exact process into the
            # lease-named scope. CUDA remains behind the existing IPC gate
            # until Broker registration verifies the scope.
            launch_command = self._scope_command_builder(
                self.lease.lease_id,
                command,
            )
        try:
            self.process = self._popen(
                launch_command,
                cwd=REPO_ROOT,
                env=env,
                pass_fds=(child_stream.fileno(),),
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            child_stream.close()
        self.stream = parent_stream
        self.pid = self.process.pid
        activated = False
        try:
            spawned = self._receive_start_frame(parent_stream, start_deadline)
            validate_message(spawned, "spawned")
            expected_selector = (
                self.lease.gpu_uuid
                if self.lease.client_environment
                else self.lease.gpu_index
            )
            if (
                spawned.get("gpu_index") != self.lease.gpu_index
                or spawned.get("expected_gpu_uuid") != self.lease.gpu_uuid
                or spawned.get("cuda_visible_devices") != expected_selector
                or spawned.get("mode") != self.mode
                or spawned.get("model") != self.model
                or spawned.get("pid") != self.pid
            ):
                raise ExecutorProtocolError("executor spawn identity mismatch")
            if activate is not None:
                activate(self.pid)
                activated = True
            send_frame(
                parent_stream,
                protocol_message(
                    "authorize_cuda",
                    lease_id=self.lease.lease_id,
                    fencing_token=self.lease.fencing_token,
                    gpu_uuid=self.lease.gpu_uuid,
                ),
            )
            # Spawn, Broker workload registration, CUDA authorization, all
            # model loads/Warp warmup and the final ready frame share one
            # deadline. A child that reaches the IPC gate but hangs during
            # preload must fail startup rather than leave readiness pending.
            message = self._receive_start_frame(parent_stream, start_deadline)
            validate_message(message, "ready")
            if (
                message.get("gpu_index") != self.lease.gpu_index
                or message.get("gpu_uuid") != self.lease.gpu_uuid
                or message.get("mode") != self.mode
                or message.get("model") != self.model
                or message.get("pid") != self.pid
            ):
                raise ExecutorProtocolError("executor readiness identity mismatch")
            probe = message.get("probe")
            if not isinstance(probe, dict) or probe.get("ready") is not True:
                raise ExecutorProtocolError("executor reported an invalid runtime probe")
            if (
                probe.get("gpu_uuid") != self.lease.gpu_uuid
                or probe.get("visible_gpu_count") != 1
                or probe.get("logical_device") != "cuda:0"
                or not isinstance(probe.get("models"), dict)
            ):
                raise ExecutorProtocolError("executor CUDA UUID differs from its lease")
            if message.get("cuda_visible_devices") != expected_selector:
                raise ExecutorProtocolError(
                    "executor CUDA visibility selector differs from its lease policy"
                )
            self.probe_payload = probe
            self.model_load_ms = float(message.get("model_load_ms", 0.0))
        except BaseException:
            if activated:
                self.close(
                    force=True,
                    prepare_termination=prepare_termination,
                )
            else:
                self._abort_unregistered_start()
            raise

    @staticmethod
    def _receive_start_frame(
        stream: socket.socket,
        deadline: float,
    ) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GpuRuntimeUnhealthy(
                "GPU executor did not complete spawn and preload before timeout"
            )
        previous_timeout = stream.gettimeout()
        stream.settimeout(remaining)
        try:
            return receive_frame(stream)
        except (TimeoutError, socket.timeout) as exc:
            raise GpuRuntimeUnhealthy(
                "GPU executor did not complete spawn and preload before timeout"
            ) from exc
        finally:
            stream.settimeout(previous_timeout)

    def _abort_unregistered_start(self) -> None:
        process = self.process
        stream = self.stream
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.shutdown(socket.SHUT_RDWR)
            stream.close()
            self.stream = None
        if process is None:
            return
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3.0)
        if process.poll() is None:
            # No workload registration means the Worker has no authority to
            # signal. Leave the process visible and fail closed for operators.
            raise GpuTerminationUnsafe(
                "unregistered executor did not exit after its IPC gate closed"
            )
        self.process = None

    def execute(
        self,
        request: JobSubmitRequest,
        output_directory: Path,
        *,
        identity: dict[str, Any],
        progress: Callable[[str, int, str | None], None],
        cancelled: Callable[[], bool],
        provenance: dict[str, Any],
        queue_wait_ms: float,
        execution_timings: dict[str, float],
    ) -> EngineExecution:
        if self.stream is None or self.process is None or self.broken:
            raise GpuRuntimeUnhealthy("GPU executor is unavailable")
        send_frame(
            self.stream,
            protocol_message(
                "execute",
                identity=identity,
                request=request.model_dump(mode="json", exclude_none=True),
                output_directory=os.fspath(output_directory),
                provenance=provenance,
                queue_wait_ms=queue_wait_ms,
                execution_timings=execution_timings,
            ),
        )
        cancellation_sent = False
        while True:
            if cancelled() and not cancellation_sent:
                send_frame(
                    self.stream,
                    protocol_message("cancel", identity=identity),
                )
                cancellation_sent = True
            readable, _, _ = select.select([self.stream], [], [], 0.05)
            if not readable:
                if self.process.poll() is not None:
                    self.broken = True
                    raise GpuRuntimeUnhealthy("GPU executor exited before returning a result")
                continue
            message = receive_frame(self.stream)
            message_type = validate_message(message)
            if message_type not in {"progress", "result", "error"}:
                self.broken = True
                raise ExecutorProtocolError("unexpected executor message during execution")
            if message.get("identity") != identity:
                self.broken = True
                raise ExecutorProtocolError("late or incorrectly fenced executor response")
            if message_type == "progress":
                progress(
                    str(message.get("stage") or "validating"),
                    int(message.get("percent", 0)),
                    message.get("message") if isinstance(message.get("message"), str) else None,
                )
                continue
            if message_type == "error":
                self.broken = bool(message.get("terminate_executor"))
                code = str(message.get("code") or "internal_error")
                if code == "cancelled":
                    raise ComputationCancelled("calculation was cancelled")
                raise ScientificComputationError(
                    code,
                    str(message.get("message") or "GPU executor failed"),
                    retryable=bool(message.get("retryable")),
                    details=(
                        dict(message["details"])
                        if isinstance(message.get("details"), dict)
                        else {}
                    ),
                )
            artifacts_raw = message.get("artifacts")
            if not isinstance(artifacts_raw, list):
                raise ExecutorProtocolError("executor result artifact list is invalid")
            artifacts: list[tuple[ArtifactDescriptor, Path]] = []
            output_root = output_directory.resolve(strict=False)
            for item in artifacts_raw:
                if not isinstance(item, dict):
                    raise ExecutorProtocolError("executor artifact entry is invalid")
                descriptor = ArtifactDescriptor.model_validate(item.get("descriptor"))
                path = Path(str(item.get("path"))).resolve(strict=False)
                if path.parent != output_root or path.name != descriptor.name:
                    raise ExecutorProtocolError("executor artifact escaped the attempt directory")
                artifacts.append((descriptor, path))
            result = message.get("result")
            timings = message.get("timings")
            if not isinstance(result, dict) or not isinstance(timings, dict):
                raise ExecutorProtocolError("executor result payload is invalid")
            return EngineExecution(
                result=dict(result),
                timings={str(key): float(value) for key, value in timings.items()},
                artifacts=tuple(artifacts),
            )

    def close(
        self,
        *,
        force: bool = False,
        prepare_termination: Callable[[], None] | None = None,
    ) -> None:
        with self._close_lock:
            process = self.process
            stream = self.stream
            if process is None:
                if stream is not None:
                    stream.close()
                    self.stream = None
                return
            if not force and process.poll() is None and stream is not None:
                with contextlib.suppress(Exception):
                    send_frame(stream, protocol_message("shutdown"))
                    ready, _, _ = select.select([stream], [], [], 2.0)
                    if ready:
                        validate_message(receive_frame(stream), "stopped")
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=3.0)
            if process.poll() is None:
                if prepare_termination is None:
                    raise GpuTerminationUnsafe(
                        "executor is still live and MPS termination was not prepared"
                    )
                # The Broker must terminate/confirm every MPS client before the
                # Worker sends even the first signal to this process group.
                prepare_termination()
                # prepare_process_termination is authoritative and normally
                # freezes and cgroup-kills the workload itself. Reap first; a
                # missing process group after that proof is expected success,
                # not evidence of an unsafe cleanup.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0.2)
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=3.0)
                if process.poll() is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=2.0)
                if process.poll() is None:
                    raise GpuTerminationUnsafe(
                        "executor process did not exit after prepared termination"
                    )
                deadline = time.monotonic() + 2.0
                while True:
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() >= deadline:
                        raise GpuTerminationUnsafe(
                            "executor process group still exists after termination"
                        )
                    time.sleep(0.02)
            else:
                with contextlib.suppress(Exception):
                    process.wait(timeout=0)
            self.process = None
            self.stream = None
            if stream is not None:
                stream.close()


ExecutorFactory = Callable[..., ExecutorHandle]


class ExecutorPool:
    """One resident primary executor plus transient, single-model overflow."""

    def __init__(
        self,
        settings: WorkerSettings,
        *,
        broker: GpuBrokerClient | None = None,
        process_factory: ExecutorFactory = SubprocessExecutor,
    ) -> None:
        self.settings = settings
        self.client_id = f"monomer-dft-{settings.deployment}-{uuid.uuid4().hex}"
        # Broker construction/auditing is a lifespan action, never a module
        # import side effect. This keeps the ASGI supervisor import CPU-only.
        self.broker: GpuBrokerClient | None = broker
        self.process_factory = process_factory
        self._primary: ExecutorHandle | None = None
        self._primary_residency: GpuLease | None = None
        self._lock = threading.RLock()
        self._execution_lock = threading.Lock()
        self._active_handle: ExecutorHandle | None = None
        self._active_execution_lease: GpuLease | None = None
        self._attempt_cleanup: dict[str, _AttemptCleanupState] = {}
        self._error: str | None = None
        self._closed = False
        self._fatal = False
        self._fatal_cleanup_proven = False
        self._suspect_resources: list[
            tuple[ExecutorHandle | None, tuple[GpuLease, ...]]
        ] = []
        self._suspect_lease_ids: set[str] = set()

    def load(self) -> None:
        self.start()

    def start(self) -> None:
        with self._lock:
            if self._fatal:
                raise GpuRuntimeUnhealthy(
                    self._error or "GPU executor pool is in a fatal state"
                )
            if self._primary is not None:
                return
            self._closed = False
            broker = self._initialize_broker()
            lease = broker.acquire(
                kind="residency",
                gpu_index=self.settings.physical_gpu,
                budget_mib=self.settings.gpu_residency_budget_mib,
                active_thread_percentage=self.settings.gpu_active_thread_percentage,
                preferred=True,
                placement="preferred",
                parent_lease_id=None,
                owner={"service": "monomer_dft", "role": "primary"},
            )
            handle = self.process_factory(
                settings=self.settings,
                lease=lease,
                mode="primary",
                model=self.settings.model_name,
            )
            try:
                handle.start(
                    activate=lambda pid: broker.activate(
                        lease,
                        pid=pid,
                        process_start_time=process_start_time(pid),
                    ),
                    prepare_termination=lambda: broker.prepare_process_termination(
                        lease
                    ),
                )
            except BaseException as start_error:
                try:
                    self._close_executor(handle, lease, force=False)
                except BaseException as cleanup_error:
                    self._poison_pool(
                        handle,
                        (lease,),
                        reason="primary executor startup cleanup was not proven",
                    )
                    raise GpuTerminationUnsafe(
                        "executor startup failed and safe cleanup was not proven"
                    ) from cleanup_error
                self._release_or_fail_closed(
                    lease,
                    handle=handle,
                    reason="primary startup lease release failed",
                )
                raise start_error
            self._primary_residency = lease
            self._primary = handle
            self._error = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._drop_primary(force=False)

    def empty_cuda_cache(self) -> None:
        # CUDA lives in a child. An OOM/fatal child is destroyed at the IPC
        # boundary; the supervisor never imports Torch just to empty a cache.
        return

    def probe(self) -> SupervisorRuntimeProbe:
        with self._lock:
            handle = self._primary
            lease = self._primary_residency
            admission_uncertain = self.admission_uncertain
            if (
                self._fatal
                or admission_uncertain
                or handle is None
                or lease is None
                or handle.broken
            ):
                return SupervisorRuntimeProbe(
                    ready=False,
                    model_loaded=False,
                    model_name=self.settings.model_name,
                    error=(
                        "GPU admission ownership is unresolved; Worker restart required"
                        if admission_uncertain
                        else self._error or "primary executor is unavailable"
                    ),
                )
            payload = dict(handle.probe_payload)
            allowed = {field.name for field in SupervisorRuntimeProbe.__dataclass_fields__.values()}
            payload = {key: value for key, value in payload.items() if key in allowed}
            payload.update(
                {
                    "ready": True,
                    "model_loaded": True,
                    "model_name": self.settings.model_name,
                    "gpu_uuid": lease.gpu_uuid,
                    "execution_path": "primary",
                }
            )
            return SupervisorRuntimeProbe(**payload)

    @property
    def admission_uncertain(self) -> bool:
        broker = self.broker
        return bool(
            broker is not None
            and getattr(broker, "admission_uncertain", False)
        )

    def execute(
        self,
        request: JobSubmitRequest,
        output_directory: Path,
        *,
        admitted: Callable[[], float | None] | None = None,
        progress: Callable[[str, int, str | None], None],
        cancelled: Callable[[], bool],
        provenance: dict[str, Any],
        queue_wait_ms: float,
    ) -> EngineExecution:
        gpu_wait_ms = 0.0
        with self._execution_lock:
            if cancelled():
                raise ComputationCancelled(
                    "calculation was cancelled before GPU admission"
                )
            if self.broker is None:
                raise ScientificComputationError(
                    "gpu_runtime_unhealthy",
                    "GPU executor pool has not completed startup.",
                    retryable=True,
                )
            if self._closed:
                raise ScientificComputationError(
                    "gpu_runtime_unhealthy",
                    "GPU executor pool is stopped.",
                    retryable=True,
                )
            handle: ExecutorHandle | None = None
            execution_lease: GpuLease | None = None
            execution_path = "primary"
            overflow = False
            capacity_errors: list[BaseException] = []
            authoritative_gpu_wait_ms: float | None = None
            if self._primary is None:
                try:
                    self.start()
                except GpuTerminationUnsafe as exc:
                    raise ScientificComputationError(
                        "cuda_fatal",
                        "Primary GPU executor recovery could not be proven safe.",
                        retryable=True,
                    ) from exc
                except (GpuCapacityUnavailable, GpuRuntimeUnhealthy) as exc:
                    capacity_errors.append(exc)
            if self._primary is not None and not self._primary.broken:
                acquire_started = time.perf_counter()
                try:
                    execution_lease = self._acquire_execution(
                        self.settings.physical_gpu,
                        budget_mib=self.settings.gpu_residency_budget_mib,
                        preferred=True,
                        placement="preferred",
                        parent_lease_id=(
                            self._primary_residency.lease_id
                            if self._primary_residency is not None
                            else None
                        ),
                        request=request,
                        wait_timeout_seconds=0.0,
                        cancelled=cancelled,
                    )
                    authoritative_gpu_wait_ms = self._admit_execution(
                        execution_lease,
                        admitted=admitted,
                        cancelled=cancelled,
                    )
                    handle = self._primary
                except (GpuCapacityUnavailable, GpuRuntimeUnhealthy) as exc:
                    capacity_errors.append(exc)
                finally:
                    gpu_wait_ms += (time.perf_counter() - acquire_started) * 1000.0
                if authoritative_gpu_wait_ms is not None:
                    gpu_wait_ms = authoritative_gpu_wait_ms
            if handle is None:
                overflow_candidates = self.settings.overflow_gpu_devices
                managed_placement = bool(
                    getattr(self.broker, "managed_placement", False)
                )
                if managed_placement and overflow_candidates:
                    # The shared Broker owns GPU3 -> GPU1 policy ordering and
                    # persistent waiter fairness. Submit one stable request.
                    overflow_candidates = overflow_candidates[:1]
                for gpu_index in overflow_candidates:
                    acquire_started = time.perf_counter()
                    try:
                        execution_lease = self._acquire_execution(
                            gpu_index,
                            budget_mib=self.settings.gpu_residency_budget_mib,
                            preferred=False,
                            placement="overflow",
                            parent_lease_id=None,
                            request=request,
                            wait_timeout_seconds=(
                                (
                                    self.settings.single_point_timeout_seconds
                                    if request.calculation_type == "single_point"
                                    else self.settings.optimization_timeout_seconds
                                )
                                if managed_placement
                                else 0.0
                            ),
                            cancelled=cancelled,
                        )
                    except (GpuCapacityUnavailable, GpuRuntimeUnhealthy, GpuBrokerError) as exc:
                        gpu_wait_ms += (time.perf_counter() - acquire_started) * 1000.0
                        capacity_errors.append(exc)
                        execution_lease = None
                        continue

                    gpu_wait_ms += (time.perf_counter() - acquire_started) * 1000.0
                    authoritative_gpu_wait_ms = self._admit_execution(
                        execution_lease,
                        admitted=admitted,
                        cancelled=cancelled,
                    )
                    if authoritative_gpu_wait_ms is not None:
                        gpu_wait_ms = authoritative_gpu_wait_ms
                    candidate = self.process_factory(
                        settings=self.settings,
                        lease=execution_lease,
                        mode="overflow",
                        model=request.model,
                    )
                    try:
                        candidate.start(
                            activate=lambda pid: self.broker.activate(
                                execution_lease,
                                pid=pid,
                                process_start_time=process_start_time(pid),
                            ),
                            prepare_termination=lambda: self.broker.prepare_process_termination(
                                execution_lease
                            ),
                        )
                    except BaseException as start_error:
                        try:
                            self._close_executor(
                                candidate,
                                execution_lease,
                                force=True,
                            )
                        except BaseException as cleanup_error:
                            self._poison_pool(
                                candidate,
                                (execution_lease,),
                                reason=(
                                    "overflow executor startup cleanup was not proven"
                                ),
                            )
                            raise ScientificComputationError(
                                "cuda_fatal",
                                "GPU executor cleanup could not be proven safe.",
                                retryable=True,
                            ) from cleanup_error
                        try:
                            self._release_or_fail_closed(
                                execution_lease,
                                handle=candidate,
                                reason="overflow startup lease release failed",
                            )
                        except GpuTerminationUnsafe as cleanup_error:
                            raise ScientificComputationError(
                                "cuda_fatal",
                                "GPU lease cleanup could not be proven safe.",
                                retryable=True,
                            ) from cleanup_error
                        raise ScientificComputationError(
                            "gpu_runtime_unhealthy",
                            "The admitted overflow GPU executor failed to start.",
                            retryable=True,
                        ) from start_error
                    handle = candidate
                    overflow = True
                    execution_path = "overflow"
                    break
            if handle is None or execution_lease is None:
                raise ScientificComputationError(
                    "gpu_capacity_unavailable",
                    "No admitted GPU execution capacity is currently available.",
                    retryable=True,
                    details={"candidates_checked": 1 + len(self.settings.overflow_gpu_devices)},
                ) from (capacity_errors[-1] if capacity_errors else None)

            identity = {
                "job_id": request.job_id,
                "attempt_token": request.attempt_token,
                "request_sha256": request.request_sha256,
                "enqueue_sequence": request.enqueue_sequence,
                "lease_id": execution_lease.lease_id,
                "gpu_uuid": execution_lease.gpu_uuid,
                "fencing_token": execution_lease.fencing_token,
            }
            execution_provenance = self._execution_provenance(
                provenance,
                request=request,
                handle=handle,
                lease=execution_lease,
                execution_path=execution_path,
                overflow=overflow,
            )
            heartbeat_stop = threading.Event()
            heartbeat_lost = threading.Event()

            def heartbeat_loop() -> None:
                while not heartbeat_stop.wait(LEASE_HEARTBEAT_SECONDS):
                    try:
                        self.broker.heartbeat(execution_lease)
                    except GpuLeaseLost:
                        heartbeat_lost.set()
                        return
                    except GpuBrokerError:
                        # The Broker keeps a live PID in suspect state. A final
                        # fenced heartbeat below decides whether the result may
                        # be accepted after connectivity returns.
                        continue

            try:
                with self._lock:
                    self._active_handle = handle
                    self._active_execution_lease = execution_lease
                if not overflow:
                    self.broker.activate(
                        execution_lease,
                        pid=handle.pid,
                        process_start_time=process_start_time(handle.pid),
                    )
                self.broker.heartbeat(execution_lease)
                heartbeat_thread = threading.Thread(
                    target=heartbeat_loop,
                    name="monomer-dft-gpu-lease-heartbeat",
                    daemon=True,
                )
                heartbeat_thread.start()
                try:
                    execution = handle.execute(
                        request,
                        output_directory,
                        identity=identity,
                        progress=progress,
                        cancelled=lambda: cancelled() or heartbeat_lost.is_set(),
                        provenance=execution_provenance,
                        queue_wait_ms=queue_wait_ms,
                        execution_timings={
                            "gpu_wait_ms": gpu_wait_ms,
                            "model_load_ms": handle.model_load_ms if overflow else 0.0,
                        },
                    )
                finally:
                    heartbeat_stop.set()
                    heartbeat_thread.join(timeout=2.0)
                if heartbeat_lost.is_set():
                    raise GpuLeaseLost("GPU lease was fenced during execution")
                # A late result is not accepted until the current fencing token
                # is confirmed after GPU synchronization in the child.
                self.broker.heartbeat(execution_lease)
                timings = dict(execution.timings)
                timings["gpu_wait_ms"] = gpu_wait_ms
                timings["model_load_ms"] = handle.model_load_ms if overflow else 0.0
                result = dict(execution.result)
                result["timings"] = dict(timings)
                result_provenance = dict(result.get("provenance") or execution_provenance)
                result_provenance.update(execution_provenance)
                result["provenance"] = result_provenance
                try:
                    artifacts = self._rewrite_scientific_result_artifact(
                        execution.artifacts,
                        result,
                    )
                except BaseException as artifact_error:
                    raise ScientificComputationError(
                        "artifact_integrity_mismatch",
                        "The authoritative scientific result artifact could not be finalized.",
                        retryable=True,
                    ) from artifact_error
                return EngineExecution(
                    result=result,
                    timings=timings,
                    artifacts=artifacts,
                )
            except ComputationCancelled as exc:
                if not heartbeat_lost.is_set():
                    raise
                self._destroy_attempt_or_fail_closed(
                    handle,
                    execution_lease,
                    overflow=overflow,
                    fatal=False,
                )
                raise ScientificComputationError(
                    "gpu_lease_lost",
                    "The GPU execution lease was fenced while the job was running.",
                    retryable=True,
                ) from exc
            except GpuLeaseLost as exc:
                self._destroy_attempt_or_fail_closed(
                    handle,
                    execution_lease,
                    overflow=overflow,
                    fatal=False,
                )
                raise ScientificComputationError(
                    "gpu_lease_lost",
                    "The GPU execution lease was lost.",
                    retryable=True,
                ) from exc
            except GpuBrokerError as exc:
                raise ScientificComputationError(
                    "gpu_runtime_unhealthy",
                    "The GPU Broker became unavailable before execution.",
                    retryable=True,
                ) from exc
            except ScientificComputationError as exc:
                if exc.code in {"gpu_oom", "cuda_fatal"} or handle.broken:
                    self._destroy_attempt_or_fail_closed(
                        handle,
                        execution_lease,
                        overflow=overflow,
                        fatal=exc.code == "cuda_fatal",
                    )
                    if exc.code == "gpu_oom":
                        # Re-establish residency only for future attempts. The
                        # failed attempt is returned exactly once and is never
                        # replayed on the replacement executor or overflow GPU.
                        try:
                            self.start()
                        except Exception as restart_error:
                            self._error = (
                                "primary executor restart after OOM failed: "
                                f"{type(restart_error).__name__}"
                            )
                            if self._fatal:
                                raise ScientificComputationError(
                                    "cuda_fatal",
                                    "Primary GPU executor recovery could not be proven safe.",
                                    retryable=True,
                                ) from restart_error
                raise
            except (ExecutorProtocolError, EOFError, OSError, GpuRuntimeUnhealthy) as exc:
                self._destroy_attempt_or_fail_closed(
                    handle,
                    execution_lease,
                    overflow=overflow,
                    fatal=False,
                )
                raise ScientificComputationError(
                    "gpu_runtime_unhealthy",
                    "The GPU executor protocol or runtime became unhealthy.",
                    retryable=True,
                ) from exc
            finally:
                try:
                    self._finalize_attempt_normally(
                        handle,
                        execution_lease,
                        overflow=overflow,
                    )
                finally:
                    with self._lock:
                        if self._active_handle is handle:
                            self._active_handle = None
                            self._active_execution_lease = None

    def _execution_provenance(
        self,
        base: dict[str, Any],
        *,
        request: JobSubmitRequest,
        handle: ExecutorHandle,
        lease: GpuLease,
        execution_path: Literal["primary", "overflow"],
        overflow: bool,
    ) -> dict[str, Any]:
        """Build provenance from the executor that actually owns cuda:0."""
        probe = dict(handle.probe_payload)
        provenance = dict(base)
        for key in (
            "aimnet_version",
            "aimnet_commit",
            "aimnet_wheel_sha256",
            "warp_version",
            "torch_version",
            "cuda_runtime",
            "gpu_name",
        ):
            if probe.get(key) is not None:
                provenance[key] = probe[key]
        if probe.get("cuda_runtime") is not None:
            provenance["cuda_version"] = probe["cuda_runtime"]
        model_details = probe.get("models", {}).get(request.model, {})
        for source, destination in (
            ("registry_key", "model_registry_key"),
            ("family", "model_family"),
            ("sha256", "model_sha256"),
        ):
            if model_details.get(source) is not None:
                provenance[destination] = model_details[source]
        provenance.update(
            {
                "model_alias": request.model,
                "model_id": request.model,
                "visible_gpu_count": int(probe.get("visible_gpu_count", 1)),
                "logical_device": "cuda:0",
                "physical_gpu": lease.gpu_index,
                "gpu_logical_device": "cuda:0",
                "gpu_physical_device": lease.gpu_index,
                "execution_path": execution_path,
                "gpu_uuid": lease.gpu_uuid,
                "gpu_budget_mib": (
                    self.settings.gpu_residency_budget_mib
                    if not overflow
                    else lease.budget_mib
                ),
                "gpu_active_thread_percentage": lease.active_thread_percentage,
                "lease_id": lease.lease_id,
                "parent_lease_id": lease.parent_lease_id,
                "fencing_token": lease.fencing_token,
                "broker_instance_id": lease.broker_instance_id,
                "gpu_preferred": lease.preferred,
            }
        )
        return provenance

    @staticmethod
    def _rewrite_scientific_result_artifact(
        artifacts: tuple[tuple[ArtifactDescriptor, Path], ...],
        result: dict[str, Any],
    ) -> tuple[tuple[ArtifactDescriptor, Path], ...]:
        rewritten: list[tuple[ArtifactDescriptor, Path]] = []
        found = False
        for descriptor, path in artifacts:
            if descriptor.artifact_id != "scientific_result":
                rewritten.append((descriptor, path))
                continue
            if found:
                raise RuntimeError("scientific result artifact is duplicated")
            found = True
            atomic_write_json(path, result)
            rewritten.append(
                (
                    describe_artifact(
                        artifact_id=descriptor.artifact_id,
                        path=path,
                        media_type=descriptor.media_type,
                    ),
                    path,
                )
            )
        if not found:
            raise RuntimeError("scientific result artifact is missing")
        return tuple(rewritten)

    def _begin_attempt_cleanup(
        self,
        lease: GpuLease,
    ) -> tuple[_AttemptCleanupState, bool]:
        with self._lock:
            state = self._attempt_cleanup.get(lease.lease_id)
            if state is not None:
                return state, False
            state = _AttemptCleanupState()
            self._attempt_cleanup[lease.lease_id] = state
            return state, True

    @staticmethod
    def _finish_attempt_cleanup(
        state: _AttemptCleanupState,
        error: BaseException | None,
    ) -> None:
        state.error = error
        state.finished.set()

    @staticmethod
    def _wait_for_attempt_cleanup(state: _AttemptCleanupState) -> None:
        if not state.finished.wait(EXECUTOR_START_TIMEOUT_SECONDS + 10.0):
            raise GpuTerminationUnsafe("timed out waiting for attempt cleanup owner")
        if state.error is not None:
            raise GpuTerminationUnsafe("attempt cleanup owner failed") from state.error

    def _finalize_attempt_normally(
        self,
        handle: ExecutorHandle,
        lease: GpuLease,
        *,
        overflow: bool,
    ) -> None:
        state, owner = self._begin_attempt_cleanup(lease)
        if not owner:
            return
        error: BaseException | None = None
        try:
            if lease.lease_id in self._suspect_lease_ids:
                return
            if overflow:
                try:
                    self._close_executor(handle, lease, force=handle.broken)
                except BaseException as cleanup_error:
                    self._poison_pool(
                        handle,
                        (lease,),
                        reason="overflow executor shutdown could not be proven",
                    )
                    raise ScientificComputationError(
                        "cuda_fatal",
                        "GPU executor cleanup could not be proven safe.",
                        retryable=True,
                    ) from cleanup_error
            try:
                self._release_or_fail_closed(
                    lease,
                    handle=None,
                    reason="execution lease release failed",
                )
            except GpuTerminationUnsafe as cleanup_error:
                raise ScientificComputationError(
                    "cuda_fatal",
                    "GPU lease cleanup could not be proven safe.",
                    retryable=True,
                ) from cleanup_error
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._finish_attempt_cleanup(state, error)

    def terminate_active(self, reason: str) -> bool:
        if reason not in {"timeout", "lease_lost", "fatal", "shutdown"}:
            raise ValueError("unsupported forced executor termination reason")
        with self._lock:
            handle = self._active_handle
            lease = self._active_execution_lease
        if handle is None or lease is None:
            return False
        self._destroy_after_failure(
            handle,
            lease,
            overflow=handle is not self._primary,
            fatal=reason == "fatal",
        )
        if reason == "timeout" and not self._fatal:
            self._rebuild_primary_bounded()
        return True

    def fatal_restart_safe(self) -> bool:
        with self._lock:
            return bool(self._fatal and self._fatal_cleanup_proven)

    def _rebuild_primary_bounded(self) -> bool:
        for attempt in range(PRIMARY_REBUILD_ATTEMPTS):
            try:
                self.start()
                return True
            except GpuTerminationUnsafe:
                raise
            except BaseException as exc:
                with self._lock:
                    self._error = (
                        "primary executor rebuild failed: "
                        f"{type(exc).__name__}"
                    )
                if attempt + 1 < PRIMARY_REBUILD_ATTEMPTS:
                    time.sleep(PRIMARY_REBUILD_BACKOFF_SECONDS * (attempt + 1))
        return False

    def _acquire_execution(
        self,
        gpu_index: str,
        *,
        budget_mib: int,
        preferred: bool,
        placement: Literal["preferred", "overflow"],
        parent_lease_id: str | None,
        request: JobSubmitRequest,
        wait_timeout_seconds: float,
        cancelled: Callable[[], bool],
    ) -> GpuLease:
        request_identity = "|".join(
            (
                request.job_id,
                request.attempt_token,
                request.request_sha256,
                str(request.enqueue_sequence),
                placement,
            )
        ).encode("utf-8")
        acquire_request_id = "dft-" + hashlib.sha256(request_identity).hexdigest()
        try:
            return self.broker.acquire(
                kind="execution",
                gpu_index=gpu_index,
                budget_mib=budget_mib,
                active_thread_percentage=self.settings.gpu_active_thread_percentage,
                preferred=preferred,
                placement=placement,
                parent_lease_id=parent_lease_id,
                owner={
                    "service": "monomer_dft",
                    "job_id": request.job_id,
                    "attempt_token": request.attempt_token,
                    "request_sha256": request.request_sha256,
                    "enqueue_sequence": request.enqueue_sequence,
                    "request_id": acquire_request_id,
                },
                wait_timeout_seconds=wait_timeout_seconds,
                cancelled=cancelled,
            )
        except GpuAcquireCancelled as exc:
            raise ComputationCancelled(
                "calculation was cancelled while waiting for GPU capacity"
            ) from exc
        except GpuLeaseLost as exc:
            raise ScientificComputationError(
                "gpu_lease_lost",
                "GPU Broker rejected a stale lease operation.",
                retryable=True,
            ) from exc
        except (GpuCapacityUnavailable, GpuRuntimeUnhealthy):
            raise
        except GpuBrokerError as exc:
            raise ScientificComputationError(
                "gpu_runtime_unhealthy",
                "The GPU Broker is unavailable for new execution leases.",
                retryable=True,
            ) from exc

    def _admit_execution(
        self,
        lease: GpuLease,
        *,
        admitted: Callable[[], float | None] | None,
        cancelled: Callable[[], bool],
    ) -> float | None:
        """Fence queued->running exactly at execution-lease admission.

        A drain/cancel that wins this boundary leaves the durable FIFO head
        queued and releases the just-acquired lease before any executor work or
        overflow model load begins.
        """

        try:
            if cancelled():
                raise ComputationCancelled(
                    "calculation was cancelled before GPU admission"
                )
            if admitted is not None:
                gpu_wait_ms = admitted()
                if gpu_wait_ms is not None:
                    return max(0.0, float(gpu_wait_ms))
            return None
        except BaseException:
            try:
                related: tuple[GpuLease, ...] = ()
                if (
                    lease.parent_lease_id is not None
                    and self._primary_residency is not None
                    and lease.parent_lease_id == self._primary_residency.lease_id
                ):
                    related = (self._primary_residency,)
                self._release_or_fail_closed(
                    lease,
                    handle=None,
                    related=related,
                    reason="pre-admission execution lease release failed",
                )
            except GpuTerminationUnsafe as cleanup_error:
                raise ScientificComputationError(
                    "cuda_fatal",
                    "GPU lease cleanup could not be proven safe.",
                    retryable=True,
                ) from cleanup_error
            raise

    def _initialize_broker(self) -> GpuBrokerClient:
        if self.broker is not None:
            return self.broker
        if self.settings.broker_enabled:
            if self.settings.broker_uds is None:
                raise ValueError("enabled GPU Broker requires a UDS path")
            broker: GpuBrokerClient = SharedGpuBrokerAdapter(
                self.settings.broker_uds,
                environment=self.settings.deployment,
                client_id=self.client_id,
                mps_pipe_root=self.settings.mps_pipe_root,
                mps_pipe_directories=(
                    self.settings.mps_pipe_directories
                ),
                dev_runtime_root=self.settings.dev_runtime_root,
            )
        else:
            if not self.settings.standalone_gpu_smoke:
                raise GpuRuntimeUnhealthy(
                    "Broker-disabled execution requires explicit standalone smoke authorization"
                )
            candidate_devices = (
                self.settings.physical_gpu,
                *self.settings.overflow_gpu_devices,
            )
            verify_host_gpu_inventory(candidate_devices)
            blocked = audit_isolated_gpu_availability(candidate_devices)
            if self.settings.physical_gpu in blocked:
                raise GpuRuntimeUnhealthy(
                    "broker-disabled dev smoke requires an idle primary GPU"
                )
            broker = DisabledBrokerClient(blocked_devices=blocked)
        self.broker = broker
        return broker

    def _destroy_after_failure(
        self,
        handle: ExecutorHandle,
        lease: GpuLease,
        *,
        overflow: bool,
        fatal: bool,
    ) -> None:
        state, owner = self._begin_attempt_cleanup(lease)
        if not owner:
            self._wait_for_attempt_cleanup(state)
            return
        error: BaseException | None = None
        try:
            self._destroy_after_failure_owned(
                handle,
                lease,
                overflow=overflow,
                fatal=fatal,
            )
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._finish_attempt_cleanup(state, error)

    def _destroy_after_failure_owned(
        self,
        handle: ExecutorHandle,
        lease: GpuLease,
        *,
        overflow: bool,
        fatal: bool,
    ) -> None:
        with self._lock:
            is_primary = not overflow and handle is self._primary
            residency = self._primary_residency if is_primary else None
            # Detach before any operation that can fail. A live executor with
            # unproven MPS cleanup must never be returned by probe() or reused.
            if is_primary:
                self._primary = None
                self._primary_residency = None
            self._error = "primary executor requires a controlled restart"
            if fatal:
                self._fatal = True
                self._closed = True
                self._fatal_cleanup_proven = False

        if is_primary and residency is None:
            self._poison_pool(
                handle,
                (lease,),
                reason="primary executor lost its residency termination authority",
            )
            raise GpuTerminationUnsafe(
                "primary executor lost its residency termination authority"
            )
        termination_authority = residency if is_primary else lease

        try:
            self._close_executor(handle, termination_authority, force=True)
        except BaseException as exc:
            leases = (lease,) if residency is None else (lease, residency)
            self._poison_pool(
                handle,
                leases,
                reason="GPU executor termination could not be proven safe",
            )
            raise GpuTerminationUnsafe(
                "GPU executor termination could not be proven safe"
            ) from exc

        if fatal:
            # Process-group disappearance was proven above. Only now may the
            # Broker quarantine the physical GPU.
            try:
                self.broker.quarantine(termination_authority, reason="cuda_fatal")
            except BaseException as exc:
                leases = (lease,) if residency is None else (lease, residency)
                self._poison_pool(
                    handle,
                    leases,
                    reason="fatal GPU quarantine could not be persisted",
                )
                raise GpuTerminationUnsafe(
                    "fatal GPU quarantine could not be persisted"
                ) from exc

        related = (
            (residency,)
            if residency is not None and residency.lease_id != lease.lease_id
            else ()
        )
        self._release_or_fail_closed(
            lease,
            handle=handle,
            related=related,
            reason="terminated executor lease release failed",
        )
        if related:
            self._release_or_fail_closed(
                residency,
                handle=handle,
                reason="terminated residency lease release failed",
            )
        if fatal:
            with self._lock:
                self._fatal_cleanup_proven = True

    def _destroy_attempt_or_fail_closed(
        self,
        handle: ExecutorHandle,
        lease: GpuLease,
        *,
        overflow: bool,
        fatal: bool,
    ) -> None:
        try:
            self._destroy_after_failure(
                handle,
                lease,
                overflow=overflow,
                fatal=fatal,
            )
        except GpuTerminationUnsafe as exc:
            raise ScientificComputationError(
                "cuda_fatal",
                "GPU executor cleanup could not be proven safe.",
                retryable=True,
            ) from exc

    def _poison_pool(
        self,
        handle: ExecutorHandle | None,
        leases: tuple[GpuLease, ...],
        *,
        reason: str,
    ) -> None:
        unique = tuple({lease.lease_id: lease for lease in leases}.values())
        with self._lock:
            if handle is not None and handle is self._primary:
                self._primary = None
                self._primary_residency = None
            self._fatal = True
            self._closed = True
            self._fatal_cleanup_proven = False
            self._error = reason
            self._suspect_resources.append((handle, unique))
            self._suspect_lease_ids.update(lease.lease_id for lease in unique)
        for suspect in unique:
            with contextlib.suppress(Exception):
                self.broker.abandon(suspect)

    def _release_or_fail_closed(
        self,
        lease: GpuLease,
        *,
        handle: ExecutorHandle | None,
        reason: str,
        related: tuple[GpuLease, ...] = (),
    ) -> None:
        try:
            self.broker.release(lease)
        except BaseException as exc:
            self._poison_pool(handle, (lease, *related), reason=reason)
            raise GpuTerminationUnsafe(reason) from exc

    def _drop_primary(self, *, force: bool) -> None:
        handle = self._primary
        residency = self._primary_residency
        if handle is None:
            self._primary = None
            self._primary_residency = None
            return
        if residency is None:
            self._poison_pool(
                handle,
                (),
                reason="primary executor lost its residency lease",
            )
            raise GpuTerminationUnsafe("primary executor lost its residency lease")
        try:
            self._close_executor(handle, residency, force=force)
        except BaseException as exc:
            self._poison_pool(
                handle,
                (residency,),
                reason="primary executor shutdown could not be proven safe",
            )
            raise GpuTerminationUnsafe(
                "primary executor shutdown could not be proven safe"
            ) from exc
        self._primary = None
        self._primary_residency = None
        self._release_or_fail_closed(
            residency,
            handle=handle,
            reason="primary residency lease release failed",
        )

    def _close_executor(
        self,
        handle: ExecutorHandle,
        lease: GpuLease,
        *,
        force: bool,
    ) -> None:
        try:
            handle.close(
                force=force,
                prepare_termination=lambda: self.broker.prepare_process_termination(
                    lease
                ),
            )
        except BaseException:
            with contextlib.suppress(Exception):
                self.broker.abandon(lease)
            raise


def executor_pool_from_settings(settings: WorkerSettings) -> ExecutorPool:
    return ExecutorPool(settings)
