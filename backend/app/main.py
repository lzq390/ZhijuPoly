from __future__ import annotations

import logging
import math
import os
from contextlib import asynccontextmanager
from time import monotonic
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.deployment_drain_middleware import DeploymentDrainMiddleware
from app.middleware import BrowserCrossSiteProtectionMiddleware
from app.postgres_database import postgres_connection
from app.postgres_preflight import (
    SCHEMA_TARGET_STARTUP,
    preflight_blockers,
    run_preflight,
)
from app.routers.assistant import router as assistant_router
from app.routers.conditional_generation import router as conditional_generation_router
from app.routers.database_browser import router as database_browser_router
from app.routers.dft import router as dft_router
from app.routers.deployment_status import router as deployment_status_router
from app.routers.gpu_status import router as gpu_status_router
from app.routers.knowledge import router as knowledge_router
from app.routers.lab_data import router as lab_data_router
from app.routers.md_demo import router as md_demo_router
from app.routers.monomer_dft import (
    MonomerDftPublicError,
    monomer_dft_public_error_handler,
    router as monomer_dft_router,
)
from app.routers.monomer_md import router as monomer_md_router
from app.routers.monomer_polymerization import router as monomer_polymerization_router
from app.routers.monomer_retrosynthesis import router as monomer_retrosynthesis_router
from app.routers.online_knowledge import router as online_knowledge_router
from app.routers.predict import router as predict_router
from app.routers.polytao import router as polytao_router
from app.routers.query import router as query_router
from app.routers.reverse_design import router as reverse_design_router
from app.services.conditional_generation_jobs import ConditionalGenerationJobManager
from app.services.conditional_generation_runtime import TorchConditionalGenerationRuntime
from app.services.deployment_control import InflightApiWriteTracker
from app.services.gpu_runtime_registry import GpuRuntimeRegistry
from app.services.image_recognition import (
    load_image_recognition_runtime,
    warmup_image_recognition_runtime,
)
from app.services.in_memory_jobs import BoundedInMemoryJobStore
from app.services.monomer_dft_download_proxy import MonomerDftDownloadProxy
from app.services.monomer_dft_reconciler import (
    MonomerDftReadinessController,
    MonomerDftReconciler,
)
from app.services.monomer_dft_repository import MonomerDftRepository
from app.services.monomer_dft_worker_client import MonomerDftWorkerClient
from app.services.monomer_retrosynthesis import (
    load_retrosynthesis_runtime,
    warmup_retrosynthesis_runtime,
)
from app.services.polytao_jobs import PolytaoJobManager
from app.services.polytao_runtime import BackendPolytaoRuntime
from app.services.monomer_md_repository import mark_expired_unclaimed_monomer_md_jobs_failed_postgres
from app.services.reverse_design_jobs import ReverseDesignJobManager
from app.services.structure_similarity_index import StructureSimilarityIndex
from gpu_resource import GpuBrokerClient, ManagedGpuLease, mps_client_environment

logger = logging.getLogger(__name__)
JOB_SHUTDOWN_GRACE_SECONDS = 35.0
_MONOMER_DFT_API_PREFIX = "/api/v1/monomer-dft"


def _is_monomer_dft_api_path(path: str) -> bool:
    return path == _MONOMER_DFT_API_PREFIX or path.startswith(
        f"{_MONOMER_DFT_API_PREFIX}/"
    )


async def health() -> dict[str, str]:
    return {"status": "ok"}


def _json_safe_validation_error(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, dict):
        return {key: _json_safe_validation_error(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_validation_error(item) for item in value]
    if isinstance(value, BaseException):
        return str(value)
    return value


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(api_app: FastAPI):
        residency_lease: ManagedGpuLease | None = None
        try:
            required_startup = api_app.state.settings.gpu_preload_mode == "required"
            _run_database_startup_preflight(api_app, required=required_startup)
            _mark_expired_monomer_md_jobs_failed(api_app)
            if api_app.state.settings.gpu_broker_enabled:
                residency_lease = await run_in_threadpool(
                    _acquire_backend_gpu_residency,
                    api_app.state.settings,
                )
                api_app.state.backend_gpu_residency_lease = residency_lease
                _initialize_dev_managed_cuda_runtime(
                    api_app.state.settings,
                    residency_lease,
                )
            if required_startup:
                api_app.state.gpu_runtime_registry.preload_enabled()
                if residency_lease is not None:
                    # Warmup may take long enough for a Broker restart or
                    # fencing response to arrive.  Never advertise a fully
                    # preloaded process whose residency is no longer proven.
                    residency_lease.assert_healthy()
                snapshot = api_app.state.gpu_runtime_registry.snapshot()
                not_ready = [
                    name
                    for name, state in snapshot["models"].items()
                    if state["enabled"] and not state["ready"]
                ]
                if snapshot["status"] != "ready" or not_ready:
                    raise RuntimeError(
                        "required GPU preload did not reach ready state: "
                        + ", ".join(sorted(not_ready))
                    )
            if residency_lease is not None:
                residency_lease.assert_healthy()
            if api_app.state.monomer_dft_readiness_controller is not None:
                api_app.state.monomer_dft_readiness_controller.start()
            yield
        finally:
            if api_app.state.monomer_dft_readiness_controller is not None:
                await api_app.state.monomer_dft_readiness_controller.stop()
            elif api_app.state.monomer_dft_reconciler is not None:
                await api_app.state.monomer_dft_reconciler.stop()
            if api_app.state.monomer_dft_worker_client is not None:
                await api_app.state.monomer_dft_worker_client.close()
            await run_in_threadpool(_shutdown_in_process_job_managers, api_app)
            if residency_lease is not None:
                # Models and their CUDA context remain resident until process
                # exit.  Stop heartbeats but let the broker reclaim only after
                # the exact owner PID is gone.
                await run_in_threadpool(residency_lease.abandon)

    app = FastAPI(title="PolyProp API", version="0.1.0", lifespan=lifespan)
    app.add_exception_handler(MonomerDftPublicError, monomer_dft_public_error_handler)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_envelope_handler(
        request: Request,
        exc: StarletteHTTPException,
    ):
        if _is_monomer_dft_api_path(request.url.path):
            if exc.status_code == 404:
                code = "route_not_found"
                message = "monomer DFT route not found"
            elif exc.status_code == 405:
                code = "method_not_allowed"
                message = "method not allowed for monomer DFT route"
            else:
                code = "http_error"
                message = "monomer DFT request failed"
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "code": code,
                    "message": message,
                    "retryable": False,
                    "details": {},
                },
                headers=exc.headers,
            )
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if _is_monomer_dft_api_path(request.url.path):
            issues: list[dict[str, str]] = []
            for error in exc.errors():
                location = ".".join(
                    str(item) for item in error.get("loc", ()) if item != "body"
                )
                issues.append(
                    {
                        "field": location[:256],
                        "type": str(error.get("type") or "validation_error")[:128],
                    }
                )
            return JSONResponse(
                status_code=422,
                content={
                    "code": "invalid_request",
                    "message": "monomer DFT request validation failed",
                    "retryable": False,
                    "details": {"issues": issues[:100]},
                },
            )
        return JSONResponse(
            status_code=422,
            content={"detail": _json_safe_validation_error(exc.errors())},
        )

    app.state.settings = app_settings
    app.state.smipoly_limiter = anyio.CapacityLimiter(1)
    app.state.postgres_connection_factory = postgres_connection
    app.state.structure_similarity_index = StructureSimilarityIndex()
    app.state.inflight_api_writes = InflightApiWriteTracker()
    # In lazy/development mode startup database checks are allowed to fail
    # without taking unrelated APIs offline.
    app.state.database_preflight_errors = ()
    app.state.backend_gpu_residency_lease = None
    app.state.conditional_generation_runtime = TorchConditionalGenerationRuntime(
        model_dir=app_settings.gen_model_dir_path,
        device=app_settings.gen_device,
    )
    app.state.polytao_runtime = BackendPolytaoRuntime(
        model_dir=app_settings.polytao_model_dir_path,
        device=app_settings.polytao_device,
        model_id=app_settings.polytao_model_id,
        model_revision=app_settings.polytao_model_revision,
    )
    app.state.gpu_runtime_registry = _build_gpu_runtime_registry(app, app_settings)
    app.state.reverse_design_job_manager = ReverseDesignJobManager(max_workers=app_settings.pi_reverse_job_workers)
    app.state.in_memory_job_store = BoundedInMemoryJobStore()
    app.state.conditional_generation_job_manager = ConditionalGenerationJobManager(
        max_workers=app_settings.gen_job_workers,
        max_active_jobs=app_settings.gen_max_active_jobs,
        store=app.state.in_memory_job_store,
    )
    app.state.polytao_job_manager = PolytaoJobManager(
        max_workers=app_settings.polytao_job_threads,
        max_active_jobs=app_settings.polytao_max_active_jobs,
        store=app.state.in_memory_job_store,
    )
    app.state.monomer_dft_repository = MonomerDftRepository(
        app_settings.app_postgres_dsn
    )
    app.state.monomer_dft_validation_limiter = anyio.CapacityLimiter(
        app_settings.monomer_dft_validation_concurrency
    )
    app.state.monomer_dft_download_proxy = MonomerDftDownloadProxy(
        spool_root=app_settings.monomer_dft_download_spool_root,
        max_concurrent=app_settings.monomer_dft_download_max_concurrent,
    )
    app.state.monomer_dft_runtime_enabled = bool(
        app_settings.monomer_dft_submit_enabled
        and app_settings.monomer_dft_worker_uds
    )
    app.state.monomer_dft_worker_client = None
    app.state.monomer_dft_reconciler = None
    app.state.monomer_dft_readiness_controller = None
    if app.state.monomer_dft_runtime_enabled:
        app.state.monomer_dft_worker_client = MonomerDftWorkerClient(
            base_url=app_settings.monomer_dft_worker_base_url,
            uds_path=app_settings.monomer_dft_worker_uds,
            timeout_seconds=app_settings.monomer_dft_worker_timeout_seconds,
            validation_limiter=app.state.monomer_dft_validation_limiter,
        )
        app.state.monomer_dft_reconciler = MonomerDftReconciler(
            repository=app.state.monomer_dft_repository,
            worker=app.state.monomer_dft_worker_client,
            interval_seconds=app_settings.monomer_dft_reconcile_interval_seconds,
            artifact_retention_days=app_settings.monomer_dft_artifact_retention_days,
        )
        app.state.monomer_dft_readiness_controller = MonomerDftReadinessController(
            repository=app.state.monomer_dft_repository,
            reconciler=app.state.monomer_dft_reconciler,
            interval_seconds=app_settings.monomer_dft_reconcile_interval_seconds,
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(BrowserCrossSiteProtectionMiddleware)
    app.add_middleware(DeploymentDrainMiddleware)

    app.add_api_route("/health", health, methods=["GET"])
    app.include_router(deployment_status_router)
    app.include_router(gpu_status_router)
    app.include_router(assistant_router)
    app.include_router(query_router)
    app.include_router(predict_router)
    app.include_router(conditional_generation_router)
    app.include_router(polytao_router)
    app.include_router(knowledge_router)
    app.include_router(lab_data_router)
    app.include_router(md_demo_router)
    app.include_router(monomer_md_router)
    app.include_router(monomer_dft_router)
    app.include_router(monomer_polymerization_router)
    app.include_router(monomer_retrosynthesis_router)
    app.include_router(online_knowledge_router)
    app.include_router(dft_router)
    app.include_router(database_browser_router)
    app.include_router(reverse_design_router)

    return app


def _acquire_backend_gpu_residency(settings: Settings) -> ManagedGpuLease:
    client = GpuBrokerClient(settings.gpu_broker_socket_path)
    client_id = f"backend-{settings.gpu_broker_environment}"
    lease = client.acquire_managed(
        kind="residency",
        placement="preferred",
        component="backend",
        environment=settings.gpu_broker_environment,
        client_id=client_id,
        memory_mib=8_192,
        thread_percent=100,
        wait_timeout_seconds=settings.gpu_broker_wait_timeout_seconds,
        heartbeat_interval_seconds=settings.gpu_broker_heartbeat_interval_seconds,
        request_id=f"backend:{settings.gpu_broker_environment}:residency",
    )
    expected_gpu = (
        (2, "GPU-89c7c52c-e252-0135-c157-24eee1a1ccbe")
        if settings.gpu_broker_environment == "prod"
        else (1, "GPU-0e19c809-f81d-a9ee-01b2-d226d00bb771")
    )
    lease_payload = lease.lease
    valid_identity = (
        isinstance(getattr(lease_payload, "lease_id", None), str)
        and bool(lease_payload.lease_id)
        and isinstance(getattr(lease_payload, "fencing_token", None), int)
        and not isinstance(lease_payload.fencing_token, bool)
        and lease_payload.fencing_token > 0
        and isinstance(getattr(lease_payload, "broker_instance_id", None), str)
        and bool(lease_payload.broker_instance_id)
        and isinstance(getattr(lease_payload, "workload_pid", None), int)
        and not isinstance(lease_payload.workload_pid, bool)
        and lease_payload.workload_pid > 0
        and isinstance(
            getattr(lease_payload, "workload_process_start_ticks", None), int
        )
        and not isinstance(lease_payload.workload_process_start_ticks, bool)
        and lease_payload.workload_process_start_ticks > 0
        and isinstance(
            getattr(lease_payload, "workload_process_group_id", None), int
        )
        and not isinstance(lease_payload.workload_process_group_id, bool)
        and lease_payload.workload_process_group_id > 0
        and isinstance(getattr(lease_payload, "workload_cgroup", None), str)
        and bool(lease_payload.workload_cgroup)
    )
    expected_metadata = {
        "kind": "residency",
        "placement": "preferred",
        "component": "backend",
        "environment": settings.gpu_broker_environment,
        "client_id": client_id,
        "gpu_index": expected_gpu[0],
        "gpu_uuid": expected_gpu[1],
        "memory_mib": 8_192,
        "thread_percent": 100,
        "preferred": True,
        "parent_lease_id": None,
        "status": "active",
    }
    if not valid_identity or any(
        getattr(lease_payload, name, object()) != expected
        for name, expected in expected_metadata.items()
    ):
        lease.close()
        raise RuntimeError("GPU broker returned invalid Backend residency lease metadata")
    try:
        os.environ.update(
            mps_client_environment(
                lease.lease,
                pipe_root=settings.gpu_mps_pipe_root,
            )
        )
    except Exception:
        lease.close()
        raise
    return lease


def _initialize_dev_managed_cuda_runtime(
    settings: Settings,
    residency_lease: ManagedGpuLease,
) -> None:
    """Initialize the dev CUDA context on the lifespan thread.

    The pinned Backend runtime must establish its MPS client before request
    work is dispatched to AnyIO threads.  Model loading remains lazy; this
    probe only creates and synchronizes the process CUDA context after the
    exact residency lease has installed its client environment.
    """

    if settings.gpu_broker_environment != "dev":
        return
    try:
        import torch

        torch.cuda.init()
        if not torch.cuda.is_initialized():
            raise RuntimeError("CUDA runtime did not reach initialized state")
        probe = torch.empty(1, device="cuda")
        torch.cuda.synchronize()
        del probe
        residency_lease.confirm_current()
    except Exception as exc:
        raise RuntimeError(
            "development Backend CUDA/MPS initialization failed"
        ) from exc


def _build_gpu_runtime_registry(app: FastAPI, settings: Settings) -> GpuRuntimeRegistry:
    def assert_gpu_admission() -> None:
        if not settings.gpu_broker_enabled:
            return
        residency = getattr(app.state, "backend_gpu_residency_lease", None)
        if residency is None:
            raise RuntimeError("GPU inference requires an active Backend residency lease")
        # A cached heartbeat can be stale for one heartbeat interval.  Every
        # inference admission therefore crosses the Broker fencing boundary
        # synchronously before any runtime is loaded or executed.
        residency.confirm_current()

    registry = GpuRuntimeRegistry(
        preload_mode=settings.gpu_preload_mode,
        max_concurrent_inferences=settings.gpu_max_concurrent_inferences,
        max_waiting_inferences=settings.gpu_max_waiting_inferences,
        admission_guard=assert_gpu_admission,
    )
    registry.register(
        "ocsr",
        enabled=settings.ocsr_enabled,
        loader=lambda: load_image_recognition_runtime(settings.ocsr_model_dir_path, settings.ocsr_device),
        warmup=warmup_image_recognition_runtime,
    )
    registry.register(
        "conditional_generation",
        enabled=settings.gen_model_enabled,
        loader=lambda: _ensure_runtime_instance(app.state.conditional_generation_runtime),
        warmup=lambda runtime: runtime.warmup(),
    )
    registry.register(
        "retrosynthesis",
        enabled=settings.retro_model_enabled,
        loader=lambda: load_retrosynthesis_runtime(settings.retro_model_id, settings.retro_device),
        warmup=warmup_retrosynthesis_runtime,
    )
    registry.register(
        "polytao",
        enabled=settings.polytao_enabled,
        loader=lambda: _ensure_runtime_instance(app.state.polytao_runtime),
        warmup=lambda runtime: runtime.warmup(),
    )
    return registry


def _ensure_runtime_instance(runtime):
    ensure_loaded = getattr(runtime, "ensure_loaded", None)
    if callable(ensure_loaded):
        return ensure_loaded()
    return runtime


def _shutdown_in_process_job_managers(
    api_app: FastAPI,
    *,
    grace_seconds: float = JOB_SHUTDOWN_GRACE_SECONDS,
) -> bool:
    """Drain all process-local job lanes within the container stop budget.

    Admission is closed for every manager first, then queued GPU tickets and
    executor work are cancelled together. Running futures receive one shared
    bounded grace period before the executors are closed. The remaining ten
    seconds of the Compose 45-second stop window are reserved for Uvicorn and
    process teardown.
    """
    managers = (
        api_app.state.conditional_generation_job_manager,
        api_app.state.polytao_job_manager,
        api_app.state.reverse_design_job_manager,
    )
    for manager in managers:
        manager.stop_accepting()
    api_app.state.gpu_runtime_registry.stop_accepting()

    futures_by_manager = [manager.cancel_pending() for manager in managers]
    deadline = monotonic() + max(0.0, float(grace_seconds))
    all_completed = True
    for manager, futures in zip(managers, futures_by_manager, strict=True):
        completed = manager.wait_for_futures(
            futures,
            timeout_seconds=max(0.0, deadline - monotonic()),
        )
        all_completed = all_completed and completed
    for manager in managers:
        manager.close_executor(wait=False)
    return all_completed


def _run_database_startup_preflight(api_app: FastAPI, *, required: bool) -> None:
    try:
        report = run_preflight(
            api_app.state.settings,
            mode="runtime",
            strict=True,
            schema_target=SCHEMA_TARGET_STARTUP,
        )
        errors = preflight_blockers(report)
    except Exception as exc:
        errors = [f"database preflight failed: {type(exc).__name__}"]
    api_app.state.database_preflight_errors = tuple(errors)
    if not errors:
        return
    message = "Backend database preflight failed: " + "; ".join(errors)
    if required:
        raise RuntimeError(message)
    logger.warning(message)


def _mark_expired_monomer_md_jobs_failed(api_app: FastAPI) -> None:
    try:
        with api_app.state.postgres_connection_factory(api_app.state.settings.app_postgres_dsn) as connection:
            mark_expired_unclaimed_monomer_md_jobs_failed_postgres(connection)
    except Exception as exc:  # pragma: no cover - runtime preflight reports database readiness.
        logger.warning("Failed to mark expired monomer MD jobs during startup: %s", exc)


app = create_app()
