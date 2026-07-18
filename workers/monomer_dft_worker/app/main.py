from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, BinaryIO, Iterator, Literal
from urllib.parse import quote

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .chemistry import ChemistryValidationError, model_capabilities
from .config import WorkerSettings, load_settings
from .executor_pool import executor_pool_from_settings
from .job_manager import JobManager, JobManagerError
from .schemas import (
    ArtifactDeletionResponse,
    CapabilitiesResponse,
    DrainResponse,
    HealthResponse,
    JobListResponse,
    JobSubmitRequest,
    PublicJobSnapshot,
)


logger = logging.getLogger("monomer_dft_worker")


def _stream_open_file(stream: BinaryIO) -> Iterator[bytes]:
    try:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            yield chunk
    finally:
        stream.close()


def _download_headers(
    *,
    filename: str,
    size_bytes: int,
    sha256: str | None = None,
) -> dict[str, str]:
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
        "Content-Length": str(size_bytes),
    }
    if sha256 is not None:
        headers["ETag"] = f'"{sha256}"'
    return headers


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": details or {},
            }
        },
        headers=headers,
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseException):
        return {"exception_type": type(value).__name__}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return {"value_type": type(value).__name__}


def create_app(
    worker_settings: WorkerSettings | None = None,
    worker_runtime: Any | None = None,
    scientific_engine: Any | None = None,
    job_manager: JobManager | None = None,
) -> FastAPI:
    settings = worker_settings or load_settings()
    if job_manager is not None:
        manager = job_manager
        runtime = worker_runtime or manager.runtime
    else:
        runtime = worker_runtime
        engine = scientific_engine
        if runtime is None and engine is None:
            # Production path: this object owns GPU child processes but imports
            # no Torch/Warp/AIMNet into the ASGI supervisor.
            runtime = executor_pool_from_settings(settings)
            engine = runtime
        elif runtime is None or engine is None:
            # Backward-compatible dependency injection for CPU-only unit tests.
            # The scientific module has no eager CUDA imports.
            if runtime is None:
                raise ValueError("an injected scientific engine requires a runtime probe")
            from .engine import AimnetComputeBackend, ScientificEngine

            engine = ScientificEngine(AimnetComputeBackend(runtime))
        manager = JobManager(
            job_root=settings.job_root,
            engine=engine,
            runtime=runtime,
            worker_version=settings.worker_version,
            max_queued_jobs=settings.max_queued_jobs,
            single_point_timeout_seconds=settings.single_point_timeout_seconds,
            optimization_timeout_seconds=settings.optimization_timeout_seconds,
            # The host wrapper restarts only this explicit status. JobManager
            # invokes it solely after a durable terminal journal and after the
            # executor pool proves MPS/process/lease cleanup safe.
            fatal_exit=lambda: os._exit(70),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "starting CPU supervisor and resident AIMNet2 executor on physical GPU %s",
            settings.physical_gpu,
        )
        manager_started = False
        try:
            await asyncio.to_thread(runtime.load)
            await manager.start()
            manager_started = True
            yield
        finally:
            if manager_started:
                await manager.stop()
            await asyncio.to_thread(runtime.close)

    app = FastAPI(
        title="NexPoly Monomer DFT Worker",
        version=settings.worker_version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.manager = manager

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        validation_errors = [
            {key: value for key, value in item.items() if key != "input"}
            for item in exc.errors()
        ]
        return _error_response(
            422,
            "invalid_request",
            "The request does not match the strict worker contract.",
            details={"validation_errors": _json_safe(validation_errors)},
        )

    @app.exception_handler(ChemistryValidationError)
    async def chemistry_validation_error(
        _request: Request,
        exc: ChemistryValidationError,
    ) -> JSONResponse:
        return _error_response(422, exc.code, str(exc), details=exc.details)

    @app.exception_handler(JobManagerError)
    async def job_manager_error(
        _request: Request,
        exc: JobManagerError,
    ) -> JSONResponse:
        retryable = exc.status_code in {429, 503}
        return _error_response(
            exc.status_code,
            exc.code,
            str(exc),
            retryable=retryable,
            headers={"Retry-After": "5"} if exc.status_code == 429 else None,
        )

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled monomer DFT worker request error", exc_info=exc)
        return _error_response(
            500,
            "internal_error",
            "The worker encountered an unexpected internal error.",
            retryable=True,
            details={"exception_type": type(exc).__name__},
        )

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        active_settings: WorkerSettings = request.app.state.settings
        probe = request.app.state.runtime.probe()
        state = request.app.state.manager.health_state()
        runtime_payload = {
            "model_name": probe.model_name,
            "model_sha256": probe.model_sha256,
            "aimnet_version": getattr(probe, "aimnet_version", None),
            "aimnet_commit": getattr(probe, "aimnet_commit", None),
            "aimnet_wheel_sha256": getattr(probe, "aimnet_wheel_sha256", None),
            "warp_version": getattr(probe, "warp_version", None),
            "torch_version": probe.torch_version,
            "cuda_runtime": probe.cuda_runtime,
            "gpu_name": probe.gpu_name,
            "visible_gpu_count": probe.visible_gpu_count,
            "logical_device": probe.logical_device,
            "models": getattr(probe, "models", {}),
            "fatal": state["fatal"],
            "fatal_reason": state["fatal_reason"],
        }
        ready = bool(probe.ready) and not state["fatal"]
        return HealthResponse(
            status="ok" if ready else "degraded",
            runtime_ready=bool(probe.ready),
            accepting_jobs=state["accepting_jobs"],
            draining=state["draining"],
            recovering=state["recovering"],
            active_jobs=state["active_jobs"],
            queued_jobs=state["queued_jobs"],
            worker_instance_id=state["worker_instance_id"],
            worker_version=active_settings.worker_version,
            runtime=runtime_payload,
        )

    @app.get("/capabilities", response_model=CapabilitiesResponse)
    async def capabilities(request: Request) -> CapabilitiesResponse:
        probe = request.app.state.runtime.probe()
        loaded = getattr(probe, "models", {})
        models = []
        for item in model_capabilities():
            model = dict(item)
            model["loaded"] = bool(
                loaded.get(model["alias"], {}).get("loaded", probe.ready)
            )
            models.append(model)
        return CapabilitiesResponse(
            models=models,
            calculation_types=["single_point", "optimization"],
            properties=["energy", "charges", "forces", "hessian", "frequencies"],
            input_limits={
                "max_heavy_atoms": 100,
                "max_total_atoms": 300,
                "max_hessian_atoms": 100,
                "max_smiles_characters": 2048,
                "single_point_timeout_seconds": request.app.state.settings.single_point_timeout_seconds,
                "optimization_timeout_seconds": request.app.state.settings.optimization_timeout_seconds,
            },
            queue={"max_concurrent_jobs": 1, "max_queued_jobs": 8},
        )

    @app.post("/drain", response_model=DrainResponse)
    async def drain(request: Request) -> DrainResponse:
        return request.app.state.manager.drain()

    @app.post("/resume", response_model=DrainResponse)
    async def resume(request: Request) -> DrainResponse:
        return request.app.state.manager.resume()

    @app.post("/jobs", response_model=PublicJobSnapshot)
    async def submit_job(
        request: Request,
        response: Response,
        payload: JobSubmitRequest,
    ) -> PublicJobSnapshot:
        manager: JobManager = request.app.state.manager
        replay = manager.replay_submission(payload)
        if replay is not None:
            response.status_code = 200
            return replay
        await asyncio.to_thread(manager.validate_submission, payload)
        # Queue mutation and asyncio.Event.set stay on the event-loop thread;
        # only RDKit parsing/prevalidation is offloaded above.
        snapshot, created = manager.submit(payload, chemistry_validated=True)
        response.status_code = 202 if created else 200
        return snapshot

    @app.get("/jobs", response_model=JobListResponse)
    async def list_jobs(
        request: Request,
        state: Literal["active", "all"] = "all",
    ) -> JobListResponse:
        return request.app.state.manager.list(state)

    @app.get("/jobs/{job_id}", response_model=PublicJobSnapshot)
    async def get_job(request: Request, job_id: str) -> PublicJobSnapshot:
        return request.app.state.manager.get(job_id)

    @app.post("/jobs/{job_id}/cancel", response_model=PublicJobSnapshot)
    async def cancel_job(
        request: Request,
        job_id: str,
        payload: JobSubmitRequest | None = None,
    ) -> PublicJobSnapshot:
        return request.app.state.manager.cancel(job_id, payload)

    @app.get("/jobs/{job_id}/artifacts/{artifact_id}")
    async def get_artifact(
        request: Request,
        job_id: str,
        artifact_id: str,
    ) -> StreamingResponse:
        access = await asyncio.to_thread(
            request.app.state.manager.artifact, job_id, artifact_id
        )
        return StreamingResponse(
            _stream_open_file(access.stream),
            media_type=access.descriptor.media_type,
            headers=_download_headers(
                filename=access.descriptor.name,
                size_bytes=access.descriptor.size_bytes,
                sha256=access.descriptor.sha256,
            ),
            background=BackgroundTask(access.stream.close),
        )

    @app.get("/jobs/{job_id}/bundle")
    async def get_bundle(request: Request, job_id: str) -> StreamingResponse:
        access = await asyncio.to_thread(request.app.state.manager.bundle, job_id)
        return StreamingResponse(
            _stream_open_file(access.stream),
            media_type="application/zip",
            headers=_download_headers(
                filename=f"{job_id}-artifacts.zip",
                size_bytes=access.size_bytes,
                sha256=access.sha256,
            ),
            background=BackgroundTask(access.stream.close),
        )

    @app.delete(
        "/jobs/{job_id}/artifacts",
        response_model=ArtifactDeletionResponse,
    )
    async def delete_artifacts(
        request: Request,
        job_id: str,
    ) -> ArtifactDeletionResponse:
        return await asyncio.to_thread(
            request.app.state.manager.delete_artifacts, job_id
        )

    return app


app = create_app()
