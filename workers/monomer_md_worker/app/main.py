from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any

from fastapi import FastAPI, HTTPException, status

from .config import WorkerSettings, load_settings
from .models import HealthResponse, JobAccepted, JobRequest
from .repository import PostgresJobRepository
from .runner import MonomerMdRunner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("monomer_md_worker")

settings: WorkerSettings = load_settings()
repository = PostgresJobRepository(settings)
runner = MonomerMdRunner(settings)
semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
active_jobs: dict[str, asyncio.Task[None]] = {}

app = FastAPI(title="NexPoly Monomer MD Worker", version=settings.worker_version)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return _build_health_response()


@app.post("/jobs", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(request: JobRequest) -> JobAccepted:
    steps = request.steps or settings.default_steps
    if steps > settings.max_steps:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"steps must be <= {settings.max_steps}",
        )
    if request.job_id in active_jobs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="job is already active on this worker",
        )

    health_response = _build_health_response()
    if settings.mode == "real" and health_response.status != "ok":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_health_rejection_message(health_response),
        )

    initial_update_ok = await _safe_update_status(
        request.job_id,
        "submitted",
        progress_percent=0,
        progress_stage="submitted",
        progress_message="Submitted to the monomer MD worker.",
    )
    if settings.db_configured and not initial_update_ok:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="monomer MD job row is not available for status updates",
        )

    task = asyncio.create_task(_run_job(request, steps))
    active_jobs[request.job_id] = task
    task.add_done_callback(lambda _: active_jobs.pop(request.job_id, None))
    return JobAccepted(
        job_id=request.job_id,
        status="submitted",
        mode=settings.mode,
        steps=steps,
        worker_id=settings.worker_id,
        worker_job_id=request.job_id,
        worker_version=settings.worker_version,
    )


def _build_health_response() -> HealthResponse:
    byteff2_root_exists = settings.byteff2_root.exists()
    runtime_ready = True
    runtime_error = None
    if settings.mode == "real":
        if byteff2_root_exists:
            runtime_ready, runtime_error = _probe_real_runtime()
        else:
            runtime_ready = False
            runtime_error = f"ByteFF2 root does not exist: {settings.byteff2_root}"

    worker_status = "ok"
    if settings.mode == "real" and (
        not byteff2_root_exists or not settings.db_configured or not runtime_ready
    ):
        worker_status = "degraded"
    return HealthResponse(
        status=worker_status,
        mode=settings.mode,
        db_configured=settings.db_configured,
        byteff2_root=str(settings.byteff2_root),
        byteff2_root_exists=byteff2_root_exists,
        runtime_ready=runtime_ready,
        runtime_error=runtime_error,
        job_root=str(settings.job_root),
        active_jobs=len(active_jobs),
        default_steps=settings.default_steps,
        max_steps=settings.max_steps,
        report_interval=settings.report_interval,
        worker_id=settings.worker_id,
        worker_version=settings.worker_version,
    )


def _probe_real_runtime() -> tuple[bool, str | None]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(settings.byteff2_root), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    try:
        completed = subprocess.run(
            [
                settings.byteff2_python,
                "-c",
                "import openmm; import MDAnalysis; import byteff2.toolkit.protocol",
            ],
            cwd=settings.byteff2_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=settings.health_probe_timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return False, f"BYTEFF2_PYTHON not found: {settings.byteff2_python}"
    except subprocess.TimeoutExpired:
        return (
            False,
            f"runtime import probe timed out after {settings.health_probe_timeout_seconds}s",
        )
    except OSError as exc:
        return False, str(exc)

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        message = detail[-1] if detail else f"runtime import probe exited {completed.returncode}"
        return False, message[:500]

    try:
        gmx_completed = subprocess.run(
            ["gmx", "--version"],
            cwd=settings.byteff2_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=settings.health_probe_timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return False, "gmx was not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"gmx probe timed out after {settings.health_probe_timeout_seconds}s"
    except OSError as exc:
        return False, str(exc)

    if gmx_completed.returncode == 0:
        return True, None
    detail = (gmx_completed.stderr or gmx_completed.stdout or "").strip().splitlines()
    message = detail[-1] if detail else f"gmx probe exited {gmx_completed.returncode}"
    return False, message[:500]


def _health_rejection_message(health_response: HealthResponse) -> str:
    if not health_response.db_configured:
        return "monomer MD worker database is not configured"
    if not health_response.byteff2_root_exists:
        return "monomer MD worker ByteFF2 root is not available"
    if not health_response.runtime_ready:
        if health_response.runtime_error:
            return f"monomer MD worker runtime is not ready: {health_response.runtime_error}"
        return "monomer MD worker runtime is not ready"
    return f"monomer MD worker health is {health_response.status}"


async def _run_job(request: JobRequest, steps: int) -> None:
    async with semaphore:
        running_update_ok = await _safe_update_status(
            request.job_id,
            "running",
            progress_percent=5,
            progress_stage="running",
            progress_message="Running the 1000-step ByteFF2 density demo.",
        )
        if settings.db_configured and not running_update_ok:
            logger.warning("monomer MD job stopped before execution: %s", request.job_id)
            return
        try:
            result = await runner.run(request, steps)
        except Exception as exc:
            logger.exception("monomer MD job failed: %s", request.job_id)
            await _safe_update_status(
                request.job_id,
                "failed",
                error=str(exc),
                progress_stage="failed",
                progress_message=str(exc)[:500],
            )
            return

        artifacts = {
            "artifact_root": str(result.output_dir),
            "outputs": result.result.get("outputs", {}),
        }
        await _safe_update_status(
            request.job_id,
            "completed",
            result=result.result,
            output_dir=str(result.output_dir),
            artifacts=artifacts,
            completed_steps=steps,
            progress_percent=100,
            progress_stage="completed",
            progress_message="Density demo completed.",
        )


async def _safe_update_status(
    job_id: str,
    status_value: str,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    output_dir: str | None = None,
    artifacts: dict[str, Any] | None = None,
    completed_steps: int | None = None,
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
            output_dir=output_dir,
            artifacts=artifacts,
            completed_steps=completed_steps,
            progress_percent=progress_percent,
            progress_stage=progress_stage,
            progress_message=progress_message,
        )
    except Exception:
        logger.exception("failed to update monomer MD job status: %s", job_id)
        return False
    if row_count == 0:
        logger.warning("monomer MD job row was not found: %s", job_id)
        return False
    return True
