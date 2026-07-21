from __future__ import annotations

from pathlib import Path
from threading import Event, Lock, Thread
from time import sleep
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import _build_gpu_runtime_registry, create_app
from app.routers import query as query_router_module
from app.routers.gpu_status import router as gpu_status_router
from app.services.gpu_runtime_registry import (
    GpuQueueFullError,
    GpuQueueTimeoutError,
    GpuRuntimeRegistry,
    GpuSchedulerClosedError,
)
from app.services.image_recognition import RecognizedStructure
from app.utils.exceptions import ModelArtifactError


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = __import__("time").monotonic() + timeout
    while not predicate():
        if __import__("time").monotonic() >= deadline:
            raise AssertionError("condition was not met before timeout")
        sleep(0.01)


def test_inference_session_runs_admission_guard_for_every_call() -> None:
    events: list[str] = []
    runtime = object()
    registry = GpuRuntimeRegistry(
        admission_guard=lambda: events.append("guard"),
    )
    registry.register(
        "ocsr",
        enabled=True,
        loader=lambda: events.append("load") or runtime,
    )

    with registry.inference_session("ocsr", timeout_seconds=1) as first:
        assert first is runtime
    with registry.inference_session("ocsr", timeout_seconds=1) as second:
        assert second is runtime

    assert events == ["guard", "load", "guard"]


def test_failed_admission_guard_skips_loader_and_releases_capacity() -> None:
    allow = False
    load_calls = 0

    def guard() -> None:
        if not allow:
            raise RuntimeError("residency fenced")

    def loader() -> object:
        nonlocal load_calls
        load_calls += 1
        return object()

    registry = GpuRuntimeRegistry(
        max_concurrent_inferences=1,
        max_waiting_inferences=0,
        admission_guard=guard,
    )
    registry.register("ocsr", enabled=True, loader=loader)

    with pytest.raises(RuntimeError, match="residency fenced"):
        with registry.inference_session("ocsr", timeout_seconds=0):
            pass
    assert load_calls == 0
    assert registry.active_inferences == 0
    assert registry.waiting_inferences == 0

    allow = True
    with registry.inference_session("ocsr", timeout_seconds=0):
        pass
    assert load_calls == 1


def test_main_registry_guard_requires_and_rechecks_healthy_residency() -> None:
    settings = Settings(
        gpu_broker_enabled=True,
        ocsr_enabled=True,
        gen_model_enabled=False,
        retro_model_enabled=False,
        polytao_enabled=False,
        model_enabled=False,
    )
    app = FastAPI()
    app.state.backend_gpu_residency_lease = None
    registry = _build_gpu_runtime_registry(app, settings)
    registry._entries["ocsr"].loader = object

    with pytest.raises(RuntimeError, match="active Backend residency"):
        with registry.inference_session("ocsr", timeout_seconds=0):
            pass

    calls = 0

    class Lease:
        def confirm_current(self) -> None:
            nonlocal calls
            calls += 1

    app.state.backend_gpu_residency_lease = Lease()
    with registry.inference_session("ocsr", timeout_seconds=0):
        pass
    with registry.inference_session("ocsr", timeout_seconds=0):
        pass
    assert calls == 2


def test_registry_preloads_enabled_models_in_registration_order() -> None:
    loaded: list[str] = []
    registry = GpuRuntimeRegistry(preload_mode="required")
    registry.register("ocsr", enabled=True, loader=lambda: loaded.append("ocsr") or object())
    registry.register("disabled", enabled=False, loader=lambda: loaded.append("disabled") or object())
    registry.register("polytao", enabled=True, loader=lambda: loaded.append("polytao") or object())

    registry.preload_enabled()

    assert loaded == ["ocsr", "polytao"]
    snapshots = registry.model_snapshots()
    assert snapshots["ocsr"]["ready"] is True
    assert snapshots["disabled"]["loaded"] is False
    assert snapshots["polytao"]["ready"] is True
    assert registry.snapshot()["status"] == "ready"


def test_registry_marks_runtime_ready_only_after_warmup() -> None:
    runtime = object()
    events: list[tuple[str, object]] = []
    registry = GpuRuntimeRegistry(preload_mode="required")
    registry.register(
        "polytao",
        enabled=True,
        loader=lambda: events.append(("load", runtime)) or runtime,
        warmup=lambda loaded: events.append(("warmup", loaded)),
    )

    registry.preload_enabled()

    assert events == [("load", runtime), ("warmup", runtime)]
    assert registry.model_snapshots()["polytao"]["ready"] is True


def test_required_warmup_failure_keeps_runtime_unready() -> None:
    registry = GpuRuntimeRegistry(preload_mode="required")

    def fail_warmup(_runtime: object) -> None:
        raise RuntimeError("CUDA warmup failed")

    registry.register(
        "polytao",
        enabled=True,
        loader=object,
        warmup=fail_warmup,
    )

    with pytest.raises(RuntimeError, match="CUDA warmup failed"):
        registry.preload_enabled()

    snapshot = registry.model_snapshots()["polytao"]
    assert snapshot["loaded"] is False
    assert snapshot["ready"] is False
    assert snapshot["error"] == "CUDA warmup failed"


def test_ocsr_inference_reuses_preloaded_runtime_and_records_success_before_release(
    monkeypatch,
) -> None:
    class ObservingRegistry(GpuRuntimeRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.active_when_success_recorded: list[int] = []

        def record_inference_success(self, name: str) -> None:
            self.active_when_success_recorded.append(self.active_inferences)
            super().record_inference_success(name)

    expected_runtime = object()
    seen_runtimes: list[object] = []
    load_calls = 0

    def load_runtime() -> object:
        nonlocal load_calls
        load_calls += 1
        return expected_runtime

    def recognize(*_args, runtime=None, **_kwargs) -> RecognizedStructure:
        seen_runtimes.append(runtime)
        return RecognizedStructure(smiles="CCO")

    registry = ObservingRegistry()
    registry.register("ocsr", enabled=True, loader=load_runtime)
    registry.preload_enabled()
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(
                ocsr_max_image_bytes=1024,
                ocsr_model_dir_path=Path("/unused"),
                ocsr_device="cuda",
                gpu_sync_queue_timeout_seconds=1.0,
            ),
            gpu_runtime_registry=registry,
        )
    )
    monkeypatch.setattr(
        query_router_module,
        "recognize_structure_image_from_bytes",
        recognize,
    )
    image = b"\x89PNG\r\n\x1a\n" + (b"\x00" * 8)

    query_router_module._run_ocsr_inference(app, image, "image/png")
    query_router_module._run_ocsr_inference(app, image, "image/png")

    assert seen_runtimes == [expected_runtime, expected_runtime]
    assert load_calls == 1
    assert registry.active_when_success_recorded == [1, 1]


def test_required_registry_rejects_production_concurrency_above_one() -> None:
    with pytest.raises(ValueError, match="must be 1"):
        GpuRuntimeRegistry(
            preload_mode="required",
            max_concurrent_inferences=2,
        )


def test_registry_records_load_failure_and_required_preload_fails() -> None:
    registry = GpuRuntimeRegistry(preload_mode="required")

    def fail() -> object:
        raise RuntimeError("checkpoint is invalid")

    registry.register("polytao", enabled=True, loader=fail)

    with pytest.raises(RuntimeError, match="checkpoint is invalid"):
        registry.preload_enabled()

    snapshot = registry.model_snapshots()["polytao"]
    assert snapshot["ready"] is False
    assert snapshot["error"] == "checkpoint is invalid"
    assert registry.snapshot()["status"] == "degraded"


def test_registry_shares_single_flight_failure_then_allows_a_later_retry() -> None:
    started = Event()
    release = Event()
    attempts_lock = Lock()
    attempts = 0
    expected_runtime = object()

    def load() -> object:
        nonlocal attempts
        with attempts_lock:
            attempts += 1
            attempt = attempts
        if attempt == 1:
            started.set()
            assert release.wait(timeout=2)
            raise RuntimeError("first load failed")
        return expected_runtime

    registry = GpuRuntimeRegistry()
    registry.register("polytao", enabled=True, loader=load)
    errors: list[BaseException] = []

    def ensure() -> None:
        try:
            registry.ensure_loaded("polytao")
        except BaseException as exc:
            errors.append(exc)

    first = Thread(target=ensure)
    second = Thread(target=ensure)
    first.start()
    assert started.wait(timeout=2)
    second.start()
    sleep(0.05)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert attempts == 1
    assert [str(error) for error in errors] == ["first load failed", "first load failed"]
    assert registry.ensure_loaded("polytao") is expected_runtime
    assert attempts == 2
    snapshot = registry.model_snapshots()["polytao"]
    assert snapshot["ready"] is True
    assert snapshot["error"] is None


def test_registry_serializes_cross_model_inference_by_default() -> None:
    registry = GpuRuntimeRegistry()
    registry.register("ocsr", enabled=True, loader=object)
    registry.register("polytao", enabled=True, loader=object)
    registry.preload_enabled()
    first_started = Event()
    release_first = Event()
    second_started = Event()

    def first() -> None:
        with registry.inference_session("ocsr", timeout_seconds=2):
            first_started.set()
            assert release_first.wait(timeout=2)

    def second() -> None:
        with registry.inference_session("polytao", timeout_seconds=2):
            second_started.set()

    first_thread = Thread(target=first)
    second_thread = Thread(target=second)
    first_thread.start()
    assert first_started.wait(timeout=2)
    second_thread.start()
    _wait_until(lambda: registry.waiting_inferences == 1)

    assert registry.active_inferences == 1
    assert second_started.is_set() is False
    release_first.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert second_started.is_set() is True
    assert registry.active_tasks == 0


def test_fifo_session_holds_capacity_across_lazy_load_and_inference() -> None:
    load_started = Event()
    release_load = Event()
    second_started = Event()
    execution_order: list[str] = []
    registry = GpuRuntimeRegistry(max_concurrent_inferences=1, max_waiting_inferences=2)

    def load_first() -> object:
        load_started.set()
        assert release_load.wait(timeout=2)
        return object()

    registry.register("ocsr", enabled=True, loader=load_first)
    registry.register("polytao", enabled=True, loader=object)

    def first() -> None:
        with registry.inference_session("ocsr", timeout_seconds=2):
            execution_order.append("ocsr")

    def second() -> None:
        with registry.inference_session("polytao", timeout_seconds=2):
            execution_order.append("polytao")
            second_started.set()

    first_thread = Thread(target=first)
    second_thread = Thread(target=second)
    first_thread.start()
    assert load_started.wait(timeout=2)
    assert registry.active_inferences == 1
    second_thread.start()
    _wait_until(lambda: registry.waiting_inferences == 1)
    assert second_started.is_set() is False
    release_load.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert execution_order == ["ocsr", "polytao"]
    assert registry.active_inferences == 0
    assert registry.waiting_inferences == 0


def test_strict_fifo_does_not_skip_a_same_model_head_waiter() -> None:
    registry = GpuRuntimeRegistry(max_concurrent_inferences=2, max_waiting_inferences=3)
    registry.register("ocsr", enabled=True, loader=object)
    registry.register("polytao", enabled=True, loader=object)
    registry.ensure_loaded("ocsr")
    registry.ensure_loaded("polytao")
    release_first = Event()
    release_second = Event()
    first_started = Event()
    second_started = Event()
    polytao_started = Event()

    def first_ocsr() -> None:
        with registry.inference_session("ocsr", timeout_seconds=2):
            first_started.set()
            assert release_first.wait(timeout=2)

    def second_ocsr() -> None:
        with registry.inference_session("ocsr", timeout_seconds=2):
            second_started.set()
            assert release_second.wait(timeout=2)

    def polytao() -> None:
        with registry.inference_session("polytao", timeout_seconds=2):
            polytao_started.set()

    threads = [Thread(target=first_ocsr), Thread(target=second_ocsr), Thread(target=polytao)]
    threads[0].start()
    assert first_started.wait(timeout=2)
    threads[1].start()
    _wait_until(lambda: registry.waiting_inferences == 1)
    threads[2].start()
    _wait_until(lambda: registry.waiting_inferences == 2)
    assert polytao_started.is_set() is False

    release_first.set()
    assert second_started.wait(timeout=2)
    assert polytao_started.wait(timeout=2)
    release_second.set()
    for thread in threads:
        thread.join(timeout=2)


def test_queue_full_timeout_and_shutdown_are_typed_without_readiness_errors() -> None:
    registry = GpuRuntimeRegistry(max_concurrent_inferences=1, max_waiting_inferences=1)
    registry.register("polytao", enabled=True, loader=object)
    registry.ensure_loaded("polytao")
    release_active = Event()
    active_started = Event()
    waiter_errors: list[BaseException] = []

    def active() -> None:
        with registry.inference_session("polytao", timeout_seconds=2):
            active_started.set()
            assert release_active.wait(timeout=2)

    def waiter() -> None:
        try:
            with registry.inference_session("polytao", timeout_seconds=2):
                pass
        except BaseException as exc:
            waiter_errors.append(exc)

    active_thread = Thread(target=active)
    waiting_thread = Thread(target=waiter)
    active_thread.start()
    assert active_started.wait(timeout=2)
    waiting_thread.start()
    _wait_until(lambda: registry.waiting_inferences == 1)

    with pytest.raises(GpuQueueFullError, match="GPU_QUEUE_FULL"):
        with registry.inference_session("polytao", timeout_seconds=1):
            pass

    registry.stop_accepting()
    waiting_thread.join(timeout=2)
    assert isinstance(waiter_errors[0], GpuSchedulerClosedError)
    release_active.set()
    active_thread.join(timeout=2)
    snapshot = registry.model_snapshots()["polytao"]
    assert snapshot["ready"] is True
    assert snapshot["error"] is None
    assert snapshot["waiting_tasks"] == 0
    assert registry.is_ready() is False
    scheduler_snapshot = registry.snapshot()
    assert scheduler_snapshot["status"] == "not_ready"
    assert scheduler_snapshot["accepting_inferences"] is False

    timeout_registry = GpuRuntimeRegistry(max_concurrent_inferences=1, max_waiting_inferences=1)
    timeout_registry.register("polytao", enabled=True, loader=object)
    timeout_registry.ensure_loaded("polytao")
    release_timeout_holder = Event()
    timeout_holder_started = Event()

    def timeout_holder() -> None:
        with timeout_registry.inference_session("polytao", timeout_seconds=2):
            timeout_holder_started.set()
            assert release_timeout_holder.wait(timeout=2)

    timeout_thread = Thread(target=timeout_holder)
    timeout_thread.start()
    assert timeout_holder_started.wait(timeout=2)
    with pytest.raises(GpuQueueTimeoutError, match="GPU_QUEUE_TIMEOUT"):
        with timeout_registry.inference_session("polytao", timeout_seconds=0.02):
            pass
    release_timeout_holder.set()
    timeout_thread.join(timeout=2)
    assert timeout_registry.waiting_inferences == 0


def test_scheduler_shutdown_status_overrides_loading_and_degraded_runtime_states() -> None:
    load_started = Event()
    release_load = Event()

    def blocking_loader():
        load_started.set()
        assert release_load.wait(timeout=2)
        return object()

    loading_registry = GpuRuntimeRegistry()
    loading_registry.register("polytao", enabled=True, loader=blocking_loader)
    loading_thread = Thread(target=lambda: loading_registry.ensure_loaded("polytao"))
    loading_thread.start()
    assert load_started.wait(timeout=2)
    loading_registry.stop_accepting()
    assert loading_registry.snapshot()["status"] == "not_ready"
    release_load.set()
    loading_thread.join(timeout=2)

    degraded_registry = GpuRuntimeRegistry()
    degraded_registry.register(
        "polytao",
        enabled=True,
        loader=lambda: (_ for _ in ()).throw(RuntimeError("checkpoint missing")),
    )
    with pytest.raises(RuntimeError, match="checkpoint missing"):
        degraded_registry.ensure_loaded("polytao")
    assert degraded_registry.snapshot()["status"] == "degraded"
    degraded_registry.stop_accepting()
    assert degraded_registry.snapshot()["status"] == "not_ready"


def test_inference_tracking_does_not_turn_business_errors_into_health_errors() -> None:
    registry = GpuRuntimeRegistry()
    registry.register("ocsr", enabled=True, loader=object)
    registry.ensure_loaded("ocsr")

    with pytest.raises(ValueError, match="invalid image"):
        with registry.track_inference("ocsr"):
            raise ValueError("invalid image")

    snapshot = registry.model_snapshots()["ocsr"]
    assert snapshot["ready"] is True
    assert snapshot["error"] is None
    assert snapshot["last_inference_error"] is None


def test_inference_diagnostics_keep_oom_recoverable(monkeypatch) -> None:
    class OutOfMemoryError(RuntimeError):
        pass

    monkeypatch.setattr("app.services.gpu_runtime_registry.release_cuda_memory", lambda: None)
    registry = GpuRuntimeRegistry()
    registry.register("polytao", enabled=True, loader=object)
    registry.ensure_loaded("polytao")

    failure_kind = registry.record_inference_failure("polytao", OutOfMemoryError("CUDA out of memory"))
    registry.record_inference_success("polytao")

    snapshot = registry.model_snapshots()["polytao"]
    assert failure_kind == "oom"
    assert snapshot["ready"] is True
    assert snapshot["error"] is None
    assert snapshot["last_inference_error"] == "CUDA out of memory"
    assert snapshot["last_inference_error_at"] is not None
    assert snapshot["last_success_at"] is not None


def test_oom_detection_handles_wrapped_cublas_allocation_failure(monkeypatch) -> None:
    released: list[bool] = []
    monkeypatch.setattr(
        "app.services.gpu_runtime_registry.release_cuda_memory",
        lambda: released.append(True),
    )
    registry = GpuRuntimeRegistry()
    registry.register("polytao", enabled=True, loader=object)
    registry.ensure_loaded("polytao")

    try:
        raise RuntimeError("CUBLAS_STATUS_ALLOC_FAILED when calling cublasCreate")
    except RuntimeError as cause:
        error = ModelArtifactError("PolyTAO inference failed")
        error.__cause__ = cause

    assert registry.record_inference_failure("polytao", error) == "oom"
    assert released == [True]
    assert registry.model_snapshots()["polytao"]["ready"] is True


def test_fatal_cuda_error_wins_over_oom_in_exception_chain(monkeypatch) -> None:
    released: list[bool] = []
    monkeypatch.setattr(
        "app.services.gpu_runtime_registry.release_cuda_memory",
        lambda: released.append(True),
    )
    registry = GpuRuntimeRegistry()
    registry.register("ocsr", enabled=True, loader=object)
    registry.ensure_loaded("ocsr")

    try:
        raise RuntimeError("CUDA out of memory")
    except RuntimeError as cause:
        error = RuntimeError("CUDA error: an illegal memory access was encountered")
        error.__cause__ = cause

    assert registry.record_inference_failure("ocsr", error) == "fatal"
    assert released == []
    assert registry.model_snapshots()["ocsr"]["ready"] is False


def test_fatal_cuda_failure_breaks_readiness_without_ready_error_conflict() -> None:
    registry = GpuRuntimeRegistry()
    registry.register("ocsr", enabled=True, loader=object)
    registry.ensure_loaded("ocsr")

    failure_kind = registry.record_inference_failure(
        "ocsr",
        RuntimeError("CUDA error: an illegal memory access was encountered"),
    )

    snapshot = registry.model_snapshots()["ocsr"]
    assert failure_kind == "fatal"
    assert snapshot["ready"] is False
    assert snapshot["error"] is not None
    assert registry.snapshot()["status"] == "degraded"
    with pytest.raises(RuntimeError, match="illegal memory access"):
        registry.ensure_loaded("ocsr")


def test_internal_gpu_status_exposes_registry_snapshot() -> None:
    registry = GpuRuntimeRegistry()
    registry.register("polytao", enabled=False, loader=object)
    app = FastAPI()
    app.state.gpu_runtime_registry = registry
    app.include_router(gpu_status_router)

    response = TestClient(app).get("/internal/gpu/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["models"]["polytao"]["enabled"] is False
    assert payload["models"]["polytao"]["waiting_tasks"] == 0
    assert payload["max_concurrent_inferences"] == 1
    assert payload["max_waiting_inferences"] == 8
    assert payload["active_inferences"] == 0
    assert payload["waiting_inferences"] == 0


def test_internal_gpu_status_degrades_on_suspect_residency_heartbeat() -> None:
    registry = GpuRuntimeRegistry(preload_mode="required")
    registry.register("polytao", enabled=True, loader=object)
    registry.preload_enabled()
    app = FastAPI()
    app.state.gpu_runtime_registry = registry
    app.state.backend_gpu_residency_lease = SimpleNamespace(
        connectivity_status="suspect",
        last_heartbeat_error="GPU broker request failed",
        lease=SimpleNamespace(
            lease_id="lease-1",
            fencing_token=1,
            broker_instance_id="broker-1",
            gpu_index=1,
            gpu_uuid="GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771",
            memory_mib=8192,
            thread_percent=100,
            status="active",
        ),
    )
    app.include_router(gpu_status_router)

    payload = TestClient(app).get("/internal/gpu/status").json()

    assert payload["status"] == "degraded"
    assert payload["accepting_inferences"] is False
    assert payload["resource_broker"]["connectivity"] == "suspect"
    assert payload["resource_broker"]["lease"]["status"] == "suspect"


def test_registry_with_no_enabled_models_is_not_ready() -> None:
    registry = GpuRuntimeRegistry()
    registry.register("polytao", enabled=False, loader=object)

    assert registry.is_ready() is False
    assert registry.snapshot()["status"] == "not_ready"


def test_polytao_job_threads_prefer_new_setting_and_keep_legacy_alias(monkeypatch) -> None:
    monkeypatch.setenv("POLYTAO_JOB_THREADS", "4")
    monkeypatch.setenv("POLYTAO_JOB_WORKERS", "2")

    settings = Settings()

    assert settings.polytao_job_threads == 4
    assert settings.polytao_job_workers == 4


def test_polytao_job_threads_fall_back_to_legacy_environment(monkeypatch) -> None:
    monkeypatch.delenv("POLYTAO_JOB_THREADS", raising=False)
    monkeypatch.setenv("POLYTAO_JOB_WORKERS", "3")

    settings = Settings()

    assert settings.polytao_job_threads == 3
    assert settings.polytao_job_workers == 3


def test_gpu_scheduler_settings_use_safe_defaults_and_explicit_overrides(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.config.DEFAULT_ENV_FILE", tmp_path / "missing.env")
    for name in (
        "GPU_MAX_CONCURRENT_INFERENCES",
        "GPU_MAX_WAITING_INFERENCES",
        "GPU_SYNC_QUEUE_TIMEOUT_SECONDS",
        "GPU_ASYNC_QUEUE_TIMEOUT_SECONDS",
        "GEN_MAX_ACTIVE_JOBS",
    ):
        monkeypatch.delenv(name, raising=False)

    defaults = Settings()
    assert defaults.gpu_max_concurrent_inferences == 1
    assert defaults.gpu_max_waiting_inferences == 8
    assert defaults.gpu_sync_queue_timeout_seconds == 30.0
    assert defaults.gpu_async_queue_timeout_seconds == 600.0
    assert defaults.gen_max_active_jobs == 8

    overridden = Settings(
        gpu_max_concurrent_inferences=2,
        gpu_max_waiting_inferences=3,
        gpu_sync_queue_timeout_seconds=4.5,
        gpu_async_queue_timeout_seconds=45.0,
        gen_max_active_jobs=6,
    )
    assert overridden.gpu_max_concurrent_inferences == 2
    assert overridden.gpu_max_waiting_inferences == 3
    assert overridden.gpu_sync_queue_timeout_seconds == 4.5
    assert overridden.gpu_async_queue_timeout_seconds == 45.0
    assert overridden.gen_max_active_jobs == 6


def test_required_preload_failure_blocks_application_startup(monkeypatch) -> None:
    monkeypatch.setattr("app.main._run_database_startup_preflight", lambda app, required: None)
    settings = Settings(
        gpu_preload_mode="required",
        ocsr_enabled=False,
        gen_model_enabled=False,
        retro_model_enabled=False,
        polytao_enabled=False,
        model_enabled=False,
    )
    app = create_app(settings)
    registry = GpuRuntimeRegistry(preload_mode="required")

    def fail() -> object:
        raise RuntimeError("required GPU runtime failed")

    registry.register("polytao", enabled=True, loader=fail)
    app.state.gpu_runtime_registry = registry

    with pytest.raises(RuntimeError, match="required GPU runtime failed"):
        with TestClient(app):
            pass


def test_required_preload_rechecks_residency_after_warmup(monkeypatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        "app.main._run_database_startup_preflight", lambda app, required: None
    )

    class Lease:
        def assert_healthy(self) -> None:
            events.append("lease-assert")
            raise RuntimeError("residency fenced during warmup")

        def abandon(self) -> None:
            events.append("lease-abandon")

    monkeypatch.setattr(
        "app.main._acquire_backend_gpu_residency", lambda _settings: Lease()
    )
    settings = Settings(
        gpu_preload_mode="required",
        gpu_broker_enabled=True,
        ocsr_enabled=False,
        gen_model_enabled=False,
        retro_model_enabled=False,
        polytao_enabled=False,
        model_enabled=False,
    )
    app = create_app(settings)
    registry = GpuRuntimeRegistry(preload_mode="required")
    registry.register(
        "polytao",
        enabled=True,
        loader=lambda: events.append("load") or object(),
        warmup=lambda _runtime: events.append("warmup"),
    )
    app.state.gpu_runtime_registry = registry

    with pytest.raises(RuntimeError, match="fenced during warmup"):
        with TestClient(app):
            pass

    assert events[:3] == ["load", "warmup", "lease-assert"]
    assert events[-1] == "lease-abandon"
