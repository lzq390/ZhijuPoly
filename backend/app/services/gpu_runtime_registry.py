from __future__ import annotations

import importlib
import importlib.metadata
import os
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Condition, Lock
from typing import Any, Callable, Iterator, Literal


RuntimeLoader = Callable[[], Any]


class GpuQueueError(RuntimeError):
    code = "GPU_QUEUE_ERROR"

    def __init__(self, message: str, *, model_name: str, retry_after_seconds: int = 1) -> None:
        super().__init__(f"{self.code}: {message}")
        self.model_name = model_name
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class GpuQueueFullError(GpuQueueError):
    """Raised when the bounded GPU inference waiting queue is full."""

    code = "GPU_QUEUE_FULL"


class GpuQueueTimeoutError(GpuQueueError):
    """Raised when a queued inference does not acquire capacity in time."""

    code = "GPU_QUEUE_TIMEOUT"


class GpuSchedulerClosedError(GpuQueueError):
    """Raised when shutdown has stopped accepting GPU inference work."""

    code = "GPU_SCHEDULER_CLOSED"


GpuQueueStoppedError = GpuSchedulerClosedError


@dataclass(frozen=True, slots=True)
class _QueueTicket:
    sequence: int
    model_name: str
    enqueued_at: float


@dataclass(frozen=True, slots=True)
class _FailureRecord:
    error_type: type[Exception]
    message: str

    def new_exception(self) -> Exception:
        try:
            return self.error_type(self.message)
        except Exception:
            return RuntimeError(f"{self.error_type.__name__}: {self.message}")


@dataclass(slots=True)
class _RuntimeEntry:
    name: str
    enabled: bool
    loader: RuntimeLoader
    loading: bool = False
    loaded: bool = False
    ready: bool = False
    error: str | None = None
    load_time_ms: float | None = None
    active_tasks: int = 0
    waiting_tasks: int = 0
    runtime: Any = None
    load_attempt: int = 0
    load_failures: dict[int, _FailureRecord] = field(default_factory=dict)
    load_error_kind: str | None = None
    load_error_retryable: bool | None = None
    fatal_inference_error: _FailureRecord | None = None
    last_inference_error: str | None = None
    last_inference_error_at: str | None = None
    last_success_at: str | None = None
    condition: Condition = field(default_factory=Condition)


class GpuRuntimeRegistry:
    """Tracks shared runtimes and schedules GPU inference in strict FIFO order."""

    def __init__(
        self,
        *,
        preload_mode: str = "lazy",
        max_concurrent_inferences: int = 1,
        max_waiting_inferences: int = 8,
    ) -> None:
        normalized = str(preload_mode or "lazy").strip().lower()
        if normalized not in {"lazy", "required"}:
            raise ValueError("GPU_PRELOAD_MODE must be one of: lazy, required")
        normalized_max_concurrent = max(1, int(max_concurrent_inferences))
        if normalized == "required" and normalized_max_concurrent != 1:
            raise ValueError(
                "GPU_MAX_CONCURRENT_INFERENCES must be 1 when GPU_PRELOAD_MODE=required"
            )
        self.preload_mode = normalized
        self.max_concurrent_inferences = normalized_max_concurrent
        self.max_waiting_inferences = max(0, int(max_waiting_inferences))
        self._entries: dict[str, _RuntimeEntry] = {}
        self._entries_lock = Lock()
        self._scheduler_condition = Condition(Lock())
        self._waiting_queue: deque[_QueueTicket] = deque()
        self._active_inferences = 0
        self._active_by_model: dict[str, int] = {}
        self._next_ticket_sequence = 0
        self._accepting_inferences = True

    def register(self, name: str, *, enabled: bool, loader: RuntimeLoader) -> None:
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("GPU runtime name must be non-empty")
        with self._entries_lock:
            if clean_name in self._entries:
                raise ValueError(f"GPU runtime is already registered: {clean_name}")
            self._entries[clean_name] = _RuntimeEntry(
                name=clean_name,
                enabled=bool(enabled),
                loader=loader,
            )

    def preload_enabled(self) -> None:
        for name in self.names():
            entry = self._entry(name)
            if entry.enabled:
                with self.inference_session(name, timeout_seconds=0):
                    pass

    @contextmanager
    def inference_session(self, name: str, *, timeout_seconds: float) -> Iterator[Any]:
        """Acquire FIFO capacity, then hold it across lazy load and inference."""
        entry = self._entry(name)
        if not entry.enabled:
            raise RuntimeError(f"GPU runtime is disabled: {name}")
        self._acquire_inference(name, timeout_seconds=max(0.0, float(timeout_seconds)))
        try:
            yield self.ensure_loaded(name)
        finally:
            self._release_inference(name)

    def stop_accepting(self) -> None:
        with self._scheduler_condition:
            self._accepting_inferences = False
            self._scheduler_condition.notify_all()

    def ensure_loaded(self, name: str) -> Any:
        """Low-level loader used by inference_session and focused loader tests."""
        entry = self._entry(name)
        if not entry.enabled:
            raise RuntimeError(f"GPU runtime is disabled: {name}")

        with entry.condition:
            if entry.loading:
                observed_attempt = entry.load_attempt
                while entry.loading and entry.load_attempt == observed_attempt:
                    entry.condition.wait()
                failure = entry.load_failures.get(observed_attempt)
                if failure is not None:
                    raise failure.new_exception()
            if entry.ready:
                return entry.runtime
            if entry.fatal_inference_error is not None:
                raise entry.fatal_inference_error.new_exception()
            entry.loading = True
            entry.load_attempt += 1
            current_attempt = entry.load_attempt
            entry.error = None
            entry.load_error_kind = None
            entry.load_error_retryable = None

        started_at = time.perf_counter()
        try:
            runtime = entry.loader()
        except Exception as exc:
            with entry.condition:
                entry.loading = False
                entry.loaded = False
                entry.ready = False
                entry.runtime = None
                entry.load_time_ms = (time.perf_counter() - started_at) * 1000
                entry.error = str(exc)[:1000]
                error_kind, error_retryable = classify_runtime_load_error(exc)
                entry.load_error_kind = error_kind
                entry.load_error_retryable = error_retryable
                entry.load_failures[current_attempt] = _failure_record(exc)
                while len(entry.load_failures) > 16:
                    entry.load_failures.pop(next(iter(entry.load_failures)))
                entry.condition.notify_all()
            if is_cuda_out_of_memory(exc):
                release_cuda_memory()
            raise

        with entry.condition:
            entry.loading = False
            entry.loaded = True
            entry.ready = True
            entry.runtime = runtime
            entry.load_time_ms = (time.perf_counter() - started_at) * 1000
            entry.error = None
            entry.load_error_kind = None
            entry.load_error_retryable = None
            entry.fatal_inference_error = None
            entry.condition.notify_all()
            return runtime

    def mark_ready(self, name: str, runtime: Any = None) -> None:
        """Record a runtime that was loaded by an existing lazy inference path."""
        entry = self._entry(name)
        with entry.condition:
            entry.loading = False
            entry.loaded = True
            entry.ready = True
            if runtime is not None:
                entry.runtime = runtime
            entry.error = None
            entry.load_error_kind = None
            entry.load_error_retryable = None
            entry.fatal_inference_error = None
            entry.condition.notify_all()

    @contextmanager
    def track_inference(self, name: str) -> Iterator[None]:
        """Count active inference only; callers classify and record runtime failures."""
        entry = self._entry(name)
        with entry.condition:
            entry.active_tasks += 1
        try:
            yield
        finally:
            with entry.condition:
                entry.active_tasks = max(0, entry.active_tasks - 1)

    def record_inference_success(self, name: str) -> None:
        entry = self._entry(name)
        with entry.condition:
            entry.last_success_at = _utc_timestamp()

    def record_inference_failure(
        self,
        name: str,
        error: BaseException,
    ) -> Literal["oom", "fatal", "runtime"]:
        """Record a runtime inference failure without conflating it with load readiness."""
        entry = self._entry(name)
        failure_kind: Literal["oom", "fatal", "runtime"]
        # A fatal CUDA context error must win if a wrapper chain also contains
        # an allocation failure.  Treating that chain as a recoverable OOM
        # would leave a dead context marked ready.
        if is_fatal_cuda_error(error):
            failure_kind = "fatal"
        elif is_cuda_out_of_memory(error):
            failure_kind = "oom"
            release_cuda_memory()
        else:
            failure_kind = "runtime"

        with entry.condition:
            entry.last_inference_error = str(error)[:1000]
            entry.last_inference_error_at = _utc_timestamp()
            if failure_kind == "fatal":
                entry.ready = False
                entry.error = str(error)[:1000]
                entry.load_error_kind = "cuda_fatal"
                entry.load_error_retryable = False
                entry.fatal_inference_error = _failure_record(error)
        return failure_kind

    def names(self) -> tuple[str, ...]:
        with self._entries_lock:
            return tuple(self._entries)

    @property
    def active_tasks(self) -> int:
        return sum(int(item["active_tasks"]) for item in self.model_snapshots().values())

    @property
    def active_inferences(self) -> int:
        with self._scheduler_condition:
            return self._active_inferences

    @property
    def waiting_inferences(self) -> int:
        with self._scheduler_condition:
            return len(self._waiting_queue)

    @property
    def accepting_inferences(self) -> bool:
        with self._scheduler_condition:
            return self._accepting_inferences

    def is_ready(self) -> bool:
        if not self.accepting_inferences:
            return False
        snapshots = self.model_snapshots()
        enabled = [item for item in snapshots.values() if item["enabled"]]
        return bool(enabled) and all(item["ready"] for item in enabled)

    def model_snapshots(self) -> dict[str, dict[str, Any]]:
        snapshots: dict[str, dict[str, Any]] = {}
        for name in self.names():
            entry = self._entry(name)
            with entry.condition:
                snapshots[name] = {
                    "enabled": entry.enabled,
                    "loading": entry.loading,
                    "loaded": entry.loaded,
                    "ready": entry.ready,
                    "load_time_ms": entry.load_time_ms,
                    "active_tasks": entry.active_tasks,
                    "waiting_tasks": entry.waiting_tasks,
                    "error": entry.error,
                    "error_kind": entry.load_error_kind,
                    "error_retryable": entry.load_error_retryable,
                    "fatal": entry.fatal_inference_error is not None,
                    "last_inference_error": entry.last_inference_error,
                    "last_inference_error_at": entry.last_inference_error_at,
                    "last_success_at": entry.last_success_at,
                }
        return snapshots

    def snapshot(self) -> dict[str, Any]:
        models = self.model_snapshots()
        with self._scheduler_condition:
            active_inferences = self._active_inferences
            waiting_inferences = len(self._waiting_queue)
            accepting_inferences = self._accepting_inferences
        enabled = [item for item in models.values() if item["enabled"]]
        if not accepting_inferences:
            status = "not_ready"
        elif any(item["loading"] for item in enabled):
            status = "loading"
        elif any(item["error"] for item in enabled):
            status = "degraded"
        elif accepting_inferences and enabled and all(item["ready"] for item in enabled):
            status = "ready"
        else:
            status = "not_ready"

        torch_version: str | None = None
        scikit_learn_version: str | None = None
        cuda_runtime: str | None = None
        device = "cpu"
        gpu_name: str | None = None
        try:
            torch = importlib.import_module("torch")
            torch_version = str(getattr(torch, "__version__", "unknown"))
            cuda_runtime_value = getattr(getattr(torch, "version", None), "cuda", None)
            cuda_runtime = str(cuda_runtime_value) if cuda_runtime_value else None
            if torch.cuda.is_available():
                device = "cuda:0"
                gpu_name = str(torch.cuda.get_device_name(0))
        except Exception:
            pass
        try:
            scikit_learn_version = importlib.metadata.version("scikit-learn")
        except importlib.metadata.PackageNotFoundError:
            pass

        return {
            "status": status,
            "build_revision": os.getenv("BUILD_REVISION", "unknown"),
            "device": device,
            "gpu_name": gpu_name,
            "torch_version": torch_version,
            "scikit_learn_version": scikit_learn_version,
            "cuda_runtime": cuda_runtime,
            "max_concurrent_inferences": self.max_concurrent_inferences,
            "max_waiting_inferences": self.max_waiting_inferences,
            "active_inferences": active_inferences,
            "waiting_inferences": waiting_inferences,
            "accepting_inferences": accepting_inferences,
            "active_tasks": sum(int(item["active_tasks"]) for item in models.values()),
            "models": models,
        }

    def _acquire_inference(self, name: str, *, timeout_seconds: float) -> None:
        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        entry = self._entry(name)
        with self._scheduler_condition:
            if not self._accepting_inferences:
                raise GpuQueueStoppedError(
                    "GPU inference runtime is shutting down",
                    model_name=name,
                )
            if self._can_start_immediately(name):
                self._activate_inference(name, entry)
                return
            if len(self._waiting_queue) >= self.max_waiting_inferences:
                raise GpuQueueFullError(
                    "GPU inference waiting queue is full",
                    model_name=name,
                )

            self._next_ticket_sequence += 1
            ticket = _QueueTicket(self._next_ticket_sequence, name, started_at)
            self._waiting_queue.append(ticket)
            with entry.condition:
                entry.waiting_tasks += 1

            while True:
                if not self._accepting_inferences:
                    self._remove_waiting_ticket(ticket, entry)
                    raise GpuQueueStoppedError(
                        "GPU inference runtime is shutting down",
                        model_name=name,
                    )
                if self._waiting_queue and self._waiting_queue[0] is ticket and self._has_capacity(name):
                    self._waiting_queue.popleft()
                    with entry.condition:
                        entry.waiting_tasks = max(0, entry.waiting_tasks - 1)
                    self._activate_inference(name, entry)
                    self._scheduler_condition.notify_all()
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._remove_waiting_ticket(ticket, entry)
                    raise GpuQueueTimeoutError(
                        f"GPU inference queue wait timed out after {timeout_seconds:g} seconds",
                        model_name=name,
                    )
                self._scheduler_condition.wait(timeout=remaining)

    def _release_inference(self, name: str) -> None:
        entry = self._entry(name)
        with self._scheduler_condition:
            self._active_inferences = max(0, self._active_inferences - 1)
            remaining = max(0, self._active_by_model.get(name, 0) - 1)
            if remaining:
                self._active_by_model[name] = remaining
            else:
                self._active_by_model.pop(name, None)
            with entry.condition:
                entry.active_tasks = max(0, entry.active_tasks - 1)
            self._scheduler_condition.notify_all()

    def _can_start_immediately(self, name: str) -> bool:
        return not self._waiting_queue and self._has_capacity(name)

    def _has_capacity(self, name: str) -> bool:
        return (
            self._active_inferences < self.max_concurrent_inferences
            and self._active_by_model.get(name, 0) < 1
        )

    def _activate_inference(self, name: str, entry: _RuntimeEntry) -> None:
        self._active_inferences += 1
        self._active_by_model[name] = self._active_by_model.get(name, 0) + 1
        with entry.condition:
            entry.active_tasks += 1

    def _remove_waiting_ticket(self, ticket: _QueueTicket, entry: _RuntimeEntry) -> None:
        try:
            self._waiting_queue.remove(ticket)
        except ValueError:
            return
        with entry.condition:
            entry.waiting_tasks = max(0, entry.waiting_tasks - 1)
        self._scheduler_condition.notify_all()

    def _entry(self, name: str) -> _RuntimeEntry:
        with self._entries_lock:
            try:
                return self._entries[name]
            except KeyError as exc:
                raise KeyError(f"GPU runtime is not registered: {name}") from exc


def is_cuda_out_of_memory(error: BaseException) -> bool:
    allocation_markers = (
        "out of memory",
        "memory allocation",
        "cublas_status_alloc_failed",
        "cuda_error_out_of_memory",
        "cuda_error_memory_allocation",
    )
    for current in _exception_chain(error):
        if current.__class__.__name__ == "OutOfMemoryError":
            return True
        message = str(current).lower()
        if ("cuda" in message or "cublas" in message) and any(
            marker in message for marker in allocation_markers
        ):
            return True
    return False


def classify_runtime_load_error(error: BaseException) -> tuple[str, bool]:
    """Classify load failures for status gating without retaining tracebacks."""
    if is_fatal_cuda_error(error):
        return "device", False
    if is_cuda_out_of_memory(error):
        return "oom", True

    messages = " ".join(str(current).lower() for current in _exception_chain(error))
    class_names = {current.__class__.__name__ for current in _exception_chain(error)}
    if "ModelArtifactError" in class_names or any(
        marker in messages
        for marker in (
            "checkpoint",
            "model file",
            "model path",
            "artifact",
            "state_dict",
            "size mismatch",
            "missing polytao",
            "dependency import",
            "not installed",
        )
    ):
        return "artifact", False
    if any(
        marker in messages
        for marker in (
            "cuda is not available",
            "cuda initialization",
            "initialization error",
            "invalid device",
            "unsupported device",
            "device ordinal",
            "device-side assert",
            "illegal memory access",
        )
    ):
        return "device", False
    if any(
        marker in messages
        for marker in (
            "temporarily unavailable",
            "resource busy",
            "timed out",
            "timeout",
            "interrupted system call",
        )
    ):
        return "transient", True
    return "unknown", False


def is_cuda_runtime_error(error: BaseException) -> bool:
    if is_cuda_out_of_memory(error):
        return True
    return any("cuda" in str(current).lower() for current in _exception_chain(error))


def is_fatal_cuda_error(error: BaseException) -> bool:
    fatal_markers = (
        "device-side assert",
        "illegal memory access",
        "context is destroyed",
        "cuda context",
        "driver shutting down",
        "initialization error",
    )
    for current in _exception_chain(error):
        message = str(current).lower()
        if "cuda" in message and any(marker in message for marker in fatal_markers):
            return True
    return False


def release_cuda_memory() -> None:
    try:
        torch = importlib.import_module("torch")
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        # Best-effort recovery must never hide the original inference failure.
        pass


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _failure_record(error: BaseException) -> _FailureRecord:
    error_type = type(error) if isinstance(error, Exception) else RuntimeError
    return _FailureRecord(error_type=error_type, message=str(error)[:1000])


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
