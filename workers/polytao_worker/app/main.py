from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, status

from .config import WorkerSettings, load_settings
from .models import HealthResponse, JobAccepted, JobRequest
from .polytao import PolytaoRuntime
from .repository import PostgresJobRepository


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("polytao_worker")

settings: WorkerSettings = load_settings()
repository = PostgresJobRepository(settings)
runtime = PolytaoRuntime(settings)
semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
active_jobs: dict[str, asyncio.Task[None]] = {}
active_jobs_lock = asyncio.Lock()

app = FastAPI(title="NexPoly PolyTAO Worker", version=settings.worker_version)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return _build_health_response()


@app.post("/jobs", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(request: JobRequest) -> JobAccepted:
    health_response = _build_health_response()
    if health_response.status != "ok":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_health_rejection_message(health_response),
        )

    async with active_jobs_lock:
        if request.job_id in active_jobs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="job is already active on this worker",
            )
        if len(active_jobs) >= settings.max_active_jobs:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="PolyTAO worker active job capacity is full",
            )

        initial_update_ok = await _safe_update_status(
            request.job_id,
            "submitted",
            progress_percent=0,
            progress_stage="submitted",
            progress_message="Submitted to the PolyTAO worker.",
        )
        if settings.db_configured and not initial_update_ok:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="PolyTAO job row is not available for status updates",
            )

        task = asyncio.create_task(_run_job(request))
        active_jobs[request.job_id] = task
        task.add_done_callback(lambda _: active_jobs.pop(request.job_id, None))

    return JobAccepted(
        job_id=request.job_id,
        status="submitted",
        mode=settings.mode,
        worker_id=settings.worker_id,
        worker_job_id=request.job_id,
        worker_version=settings.worker_version,
    )


def _build_health_response() -> HealthResponse:
    probe = runtime.probe()
    worker_status = "ok"
    if (
        settings.mode != "real"
        or not settings.db_configured
        or not probe.model_files_ready
        or not probe.runtime_ready
    ):
        worker_status = "degraded"

    return HealthResponse(
        status=worker_status,
        mode=settings.mode,
        db_configured=settings.db_configured,
        model_dir=str(settings.model_dir),
        model_files_ready=probe.model_files_ready,
        runtime_ready=probe.runtime_ready,
        runtime_error=probe.runtime_error,
        active_jobs=len(active_jobs),
        model_id=settings.model_id,
        model_revision=settings.model_revision,
        default_params={
            "candidate_count": settings.default_candidate_count,
            "temperature": settings.default_temperature,
            "top_k": settings.default_top_k,
            "top_p": settings.default_top_p,
            "max_length": settings.default_max_length,
        },
        worker_id=settings.worker_id,
        worker_version=settings.worker_version,
    )


def _health_rejection_message(health_response: HealthResponse) -> str:
    if health_response.mode != "real":
        return f"PolyTAO worker mode is {health_response.mode}; only real mode accepts jobs"
    if not health_response.db_configured:
        return "PolyTAO worker database is not configured"
    if not health_response.model_files_ready:
        return health_response.runtime_error or "PolyTAO model files are not available"
    if not health_response.runtime_ready:
        if health_response.runtime_error:
            return f"PolyTAO worker runtime is not ready: {health_response.runtime_error}"
        return "PolyTAO worker runtime is not ready"
    return f"PolyTAO worker health is {health_response.status}"


async def _run_job(request: JobRequest) -> None:
    async with semaphore:
        running_update_ok = await _safe_update_status(
            request.job_id,
            "running",
            progress_percent=5,
            progress_stage="running",
            progress_message="Generating PolyTAO polymer candidates.",
        )
        if settings.db_configured and not running_update_ok:
            logger.warning("PolyTAO job stopped before execution: %s", request.job_id)
            return
        try:
            generation = await asyncio.to_thread(
                runtime.generate,
                prompt=request.prompt,
                candidate_count=request.candidate_count,
                temperature=request.temperature,
                top_k=request.top_k,
                top_p=request.top_p,
                max_length=request.max_length,
            )
        except Exception as exc:
            logger.exception("PolyTAO job failed: %s", request.job_id)
            await _safe_update_status(
                request.job_id,
                "failed",
                error=str(exc),
                progress_stage="failed",
                progress_message=str(exc)[:500],
            )
            return

        await _safe_update_status(
            request.job_id,
            "completed",
            result=generation.result,
            returned_count=generation.returned_count,
            progress_percent=100,
            progress_stage="completed",
            progress_message="PolyTAO generation completed.",
        )


async def _safe_update_status(
    job_id: str,
    status_value: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    returned_count: int | None = None,
    progress_percent: int | None = None,
    progress_stage: str | None = None,
    progress_message: str | None = None,
) -> bool:
    if not settings.db_configured:
        return True
    try:
        row_count = await asyncio.to_thread(
            repository.update_status,
            job_id,
            status_value,
            result=result,
            error=error,
            returned_count=returned_count,
            progress_percent=progress_percent,
            progress_stage=progress_stage,
            progress_message=progress_message,
        )
    except Exception:
        logger.exception("failed to update PolyTAO job status: %s", job_id)
        return False
    if row_count == 0:
        logger.warning("PolyTAO job row was not found: %s", job_id)
        return False
    return True
