from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status

from .config import WorkerSettings, load_settings
from .models import ArtifactDeletionResponse, DrainResponse, HealthResponse, JobAccepted, JobRequest
from .repository import JobUpdateResult, PostgresJobRepository
from .runner import MonomerMdRunner
from .runtime_health import (
    RuntimeSnapshot,
    degraded_runtime_snapshot,
    initial_runtime_snapshot,
    probe_runtime_snapshot,
)
from gpu_resource import GpuBrokerClient, GpuBrokerClientError, ManagedGpuLease
from scripts.worker_slot_runtime import (
    PRODUCTION_RUNTIME_ROOT,
    PRODUCTION_SOURCE_ROOT,
    WorkerSlotError,
    verify_runtime_binding,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("monomer_md_worker")


_WORKER_MODULE_PATH = Path("workers/monomer_md_worker/app/main.py")


@dataclass(frozen=True)
class RuntimeIdentity:
    source_sha: str | None
    source_tree: str | None
    source_root: str
    venv_prefix: str
    venv_slot: str | None
    worker_lock_sha256: str | None
    slot_record_sha256: str | None
    base_python_identity_sha256: str | None
    python_executable: str


def _load_runtime_identity(
    *,
    module_path: Path | None = None,
    python_prefix: Path | None = None,
    python_executable: Path | None = None,
    production_source_root: Path = PRODUCTION_SOURCE_ROOT,
    runtime_root: Path = PRODUCTION_RUNTIME_ROOT,
) -> RuntimeIdentity:
    """Derive source and A/B runtime identity without trusting environment values."""

    try:
        loaded_module = (module_path or Path(__file__)).resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("cannot resolve the Monomer MD Worker source") from exc
    try:
        source_root = loaded_module.parents[3]
    except IndexError as exc:
        raise RuntimeError("Monomer MD Worker source is outside the release layout") from exc
    if loaded_module != source_root / _WORKER_MODULE_PATH:
        raise RuntimeError("Monomer MD Worker source has an unexpected layout")

    try:
        resolved_prefix = (python_prefix or Path(sys.prefix)).resolve(strict=True)
        resolved_executable = (python_executable or Path(sys.executable)).resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("cannot resolve the Monomer MD Worker Python runtime") from exc

    source_sha: str | None = None
    source_tree: str | None = None
    venv_slot: str | None = None
    worker_lock_sha256: str | None = None
    slot_record_sha256: str | None = None
    base_python_identity_sha256: str | None = None
    try:
        resolved_production_root = production_source_root.resolve(strict=True)
    except OSError:
        resolved_production_root = production_source_root
    if source_root == resolved_production_root:
        try:
            checkout, selection, selected_python = verify_runtime_binding(
                source_root=production_source_root,
                runtime_root=runtime_root,
            )
            expected_prefix = Path(selection.slot.venv_prefix).resolve(strict=True)
            expected_executable = selected_python.resolve(strict=True)
        except (OSError, WorkerSlotError) as exc:
            raise RuntimeError("production Worker A/B runtime identity is invalid") from exc
        if resolved_prefix != expected_prefix:
            raise RuntimeError("production Worker is not running from the active A/B venv")
        if resolved_executable != expected_executable:
            raise RuntimeError("production Worker executable differs from the active A/B venv")
        source_sha = checkout.source_sha
        source_tree = checkout.source_tree
        venv_slot = selection.active.slot
        worker_lock_sha256 = selection.active.worker_lock_sha256
        slot_record_sha256 = selection.active.slot_record_sha256
        base_python_identity_sha256 = selection.slot.base_python_identity_sha256

    return RuntimeIdentity(
        source_sha=source_sha,
        source_tree=source_tree,
        source_root=str(source_root),
        venv_prefix=str(resolved_prefix),
        venv_slot=venv_slot,
        worker_lock_sha256=worker_lock_sha256,
        slot_record_sha256=slot_record_sha256,
        base_python_identity_sha256=base_python_identity_sha256,
        python_executable=str(resolved_executable),
    )


runtime_identity = _load_runtime_identity()
settings: WorkerSettings = load_settings()
repository = PostgresJobRepository(settings)
runner = MonomerMdRunner(settings)
semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
active_jobs: dict[str, asyncio.Task[None]] = {}
active_formal_jobs: set[str] = set()
active_jobs_lock = asyncio.Lock()
worker_instance_id = uuid4().hex
recovery_ready = not settings.db_configured
draining = False
shutting_down = False
heartbeat_task: asyncio.Task[None] | None = None
recovery_task: asyncio.Task[None] | None = None
runtime_snapshot: RuntimeSnapshot = initial_runtime_snapshot(settings)
runtime_probe_initialized = False
BROKER_HEALTH_CLIENT_TIMEOUT_SECONDS = 0.1
BROKER_HEALTH_WALL_TIMEOUT_SECONDS = 0.2


@asynccontextmanager
async def lifespan(_: FastAPI):
    global heartbeat_task, recovery_task, shutting_down, draining
    shutting_down = False
    draining = False
    await asyncio.gather(
        _initialize_runtime_snapshot(),
        _attempt_recovery(),
    )
    if settings.db_configured and not recovery_ready:
        recovery_task = asyncio.create_task(_recovery_loop())
    heartbeat_task = asyncio.create_task(_heartbeat_loop())
    try:
        yield
    finally:
        shutting_down = True
        draining = True
        for background_task in (heartbeat_task, recovery_task):
            if background_task is not None:
                background_task.cancel()
        tasks = list(active_jobs.values())
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=15,
                )
            except asyncio.TimeoutError:
                logger.error("timed out waiting for active monomer MD tasks during shutdown")
        if settings.db_configured:
            try:
                await asyncio.to_thread(
                    repository.fail_instance_jobs,
                    worker_instance_id,
                    message="Monomer MD worker shut down before this job finished.",
                    error_category="worker_shutdown",
                )
            except Exception:
                logger.exception("failed to mark active monomer MD jobs during shutdown")


app = FastAPI(
    title="NexPoly Monomer MD Worker",
    version=settings.worker_version,
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return await _build_health_response()


@app.post("/drain", response_model=DrainResponse)
async def drain_worker() -> DrainResponse:
    global draining
    draining = True
    async with active_jobs_lock:
        active_job_count = len(active_jobs)
    return DrainResponse(
        status="draining",
        accepting_jobs=False,
        active_jobs=active_job_count,
        worker_instance_id=worker_instance_id,
    )


@app.post("/resume", response_model=DrainResponse)
async def resume_worker() -> DrainResponse:
    global draining
    draining = False
    async with active_jobs_lock:
        active_job_count = len(active_jobs)
    return DrainResponse(
        status="ready",
        accepting_jobs=(await _build_health_response()).accepting_jobs,
        active_jobs=active_job_count,
        worker_instance_id=worker_instance_id,
    )


@app.post("/jobs", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def submit_job(request: JobRequest) -> JobAccepted:
    steps = request.steps or settings.default_steps
    if request.run_mode == "demo" and steps > settings.max_steps:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"steps must be <= {settings.max_steps}",
        )
    health_response = await _build_health_response()
    rejection = _job_rejection_message(health_response, request)
    if rejection is not None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=rejection)

    async with active_jobs_lock:
        if draining:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="monomer MD worker is draining for deployment",
            )
        if not recovery_ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="monomer MD worker database recovery has not completed",
            )
        if request.job_id in active_jobs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="job is already active on this worker",
            )
        if len(active_jobs) >= settings.max_active_jobs:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="monomer MD worker active job capacity is full",
            )
        if request.run_mode == "formal" and active_formal_jobs:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="formal ByteFF2 monomer MD capacity is full",
            )

        initial_update_result = await _safe_update_status(
            request.job_id,
            "submitted",
            progress_percent=0,
            progress_stage="submitted",
            progress_message="Submitted to the monomer MD worker.",
        )
        if settings.db_configured and initial_update_result is not JobUpdateResult.UPDATED:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="monomer MD job row is not available for status updates",
            )

        task = asyncio.create_task(_run_job(request, steps))
        active_jobs[request.job_id] = task
        if request.run_mode == "formal":
            active_formal_jobs.add(request.job_id)
        task.add_done_callback(lambda _: _remove_active_job(request.job_id))
    return JobAccepted(
        job_id=request.job_id,
        status="submitted",
        mode=settings.mode,
        steps=steps,
        worker_id=settings.worker_id,
        worker_job_id=request.job_id,
        worker_version=settings.worker_version,
    )


def _remove_active_job(job_id: str) -> None:
    active_jobs.pop(job_id, None)
    active_formal_jobs.discard(job_id)


@app.delete("/jobs/{job_id}/artifacts", response_model=ArtifactDeletionResponse)
async def delete_job_artifacts(job_id: str) -> ArtifactDeletionResponse:
    async with active_jobs_lock:
        if job_id in active_jobs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="cannot delete artifacts for an active monomer MD job",
            )
    artifact_root = runner.output_dir_for_job(job_id)
    if artifact_root.exists():
        await asyncio.to_thread(shutil.rmtree, artifact_root)
        return ArtifactDeletionResponse(
            job_id=job_id,
            deleted=True,
            artifact_root=str(artifact_root),
            message="artifacts deleted",
        )
    return ArtifactDeletionResponse(
        job_id=job_id,
        deleted=False,
        artifact_root=str(artifact_root),
        message="artifacts were already absent",
    )


async def _build_health_response() -> HealthResponse:
    snapshot = runtime_snapshot
    byteff2_root_exists = snapshot.byteff2_root_exists
    runtime_ready = snapshot.runtime_ready
    runtime_error = snapshot.runtime_error
    protocols = snapshot.protocols_dict()
    gpu_broker_ready = True
    gpu_broker_error: str | None = None
    if settings.gpu_broker_enabled:
        try:
            broker_status = await asyncio.wait_for(
                asyncio.to_thread(_read_gpu_broker_status),
                timeout=BROKER_HEALTH_WALL_TIMEOUT_SECONDS,
            )
            if broker_status.get("draining") is True:
                raise GpuBrokerClientError("broker_draining", "GPU broker is draining")
        except asyncio.TimeoutError:
            gpu_broker_ready = False
            gpu_broker_error = "broker_timeout: GPU broker status timed out"
        except GpuBrokerClientError as exc:
            gpu_broker_ready = False
            gpu_broker_error = f"{exc.code}: {exc}"
        except Exception:
            gpu_broker_ready = False
            gpu_broker_error = "broker_unavailable: GPU broker status failed"
        if getattr(runner, "gpu_admission_uncertain", False):
            gpu_broker_ready = False
            gpu_broker_error = (
                "broker_admission_uncertain: restart required after unresolved admission"
            )
    worker_status = "ok"
    if settings.mode == "real" and (
        not byteff2_root_exists
        or not settings.db_configured
        or not runtime_ready
    ):
        worker_status = "degraded"
    if settings.db_configured and not recovery_ready:
        worker_status = "degraded"
    if settings.mode == "real" and settings.gpu_broker_enabled and not gpu_broker_ready:
        worker_status = "degraded"
    accepting_jobs = (
        worker_status == "ok"
        and recovery_ready
        and not draining
        and len(active_jobs) < settings.max_active_jobs
    )
    return HealthResponse(
        status=worker_status,
        mode=settings.mode,
        source_sha=runtime_identity.source_sha,
        source_tree=runtime_identity.source_tree,
        source_root=runtime_identity.source_root,
        venv_prefix=runtime_identity.venv_prefix,
        venv_slot=runtime_identity.venv_slot,
        worker_lock_sha256=runtime_identity.worker_lock_sha256,
        slot_record_sha256=runtime_identity.slot_record_sha256,
        base_python_identity_sha256=runtime_identity.base_python_identity_sha256,
        python_executable=runtime_identity.python_executable,
        db_configured=settings.db_configured,
        byteff2_root=str(settings.byteff2_root),
        byteff2_root_exists=byteff2_root_exists,
        runtime_ready=runtime_ready,
        runtime_error=runtime_error,
        job_root=str(settings.job_root),
        active_jobs=len(active_jobs),
        max_active_jobs=settings.max_active_jobs,
        worker_instance_id=worker_instance_id,
        accepting_jobs=accepting_jobs,
        draining=draining,
        lease_seconds=settings.lease_seconds,
        default_steps=settings.default_steps,
        max_steps=settings.max_steps,
        report_interval=settings.report_interval,
        worker_id=settings.worker_id,
        worker_version=settings.worker_version,
        protocols=protocols,
        cuda_visible_devices=settings.cuda_visible_devices,
        gpu_broker_enabled=settings.gpu_broker_enabled,
        gpu_broker_ready=gpu_broker_ready,
        gpu_broker_error=gpu_broker_error,
    )


def _read_gpu_broker_status() -> dict[str, Any]:
    return GpuBrokerClient(
        settings.gpu_broker_socket_path,
        timeout_seconds=BROKER_HEALTH_CLIENT_TIMEOUT_SECONDS,
    ).status()


async def _initialize_runtime_snapshot() -> None:
    global runtime_snapshot, runtime_probe_initialized
    if runtime_probe_initialized:
        return
    runtime_probe_initialized = True
    try:
        runtime_snapshot = await probe_runtime_snapshot(
            settings,
            runner=runner,
            worker_instance_id=worker_instance_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("failed to initialize the monomer MD runtime snapshot")
        runtime_snapshot = degraded_runtime_snapshot(
            settings,
            "monomer MD runtime startup probe failed",
        )


def _health_rejection_message(health_response: HealthResponse) -> str:
    if health_response.gpu_broker_enabled and not health_response.gpu_broker_ready:
        if health_response.gpu_broker_error:
            return f"monomer MD GPU broker is not ready: {health_response.gpu_broker_error}"
        return "monomer MD GPU broker is not ready"
    if not health_response.db_configured:
        return "monomer MD worker database is not configured"
    if not recovery_ready:
        return "monomer MD worker database recovery has not completed"
    if not health_response.byteff2_root_exists:
        return "monomer MD worker ByteFF2 root is not available"
    if not health_response.runtime_ready:
        if health_response.runtime_error:
            return f"monomer MD worker runtime is not ready: {health_response.runtime_error}"
        return "monomer MD worker runtime is not ready"
    return f"monomer MD worker health is {health_response.status}"


def _job_rejection_message(health_response: HealthResponse, request: JobRequest) -> str | None:
    if request.run_mode == "formal" and settings.mode != "real":
        return "formal ByteFF2 protocols require real worker mode"
    if settings.mode == "real" and health_response.status != "ok":
        return _health_rejection_message(health_response)
    if health_response.draining:
        return "monomer MD worker is draining for deployment"
    if not health_response.accepting_jobs:
        if not recovery_ready:
            return "monomer MD worker database recovery has not completed"
        # Capacity is rechecked while holding active_jobs_lock so callers retain
        # the established 429 response instead of a racy generic 503.
        if health_response.active_jobs >= health_response.max_active_jobs:
            return None
        return "monomer MD worker is not accepting jobs"
    if request.run_mode == "formal":
        protocol_health = health_response.protocols.get(request.protocol)
        if not isinstance(protocol_health, dict):
            return f"ByteFF2 {request.protocol} readiness is not reported"
        if protocol_health.get("runtime_ready") is not True:
            error = protocol_health.get("runtime_error")
            if error:
                return f"ByteFF2 {request.protocol} runtime is not ready: {error}"
            return f"ByteFF2 {request.protocol} runtime is not ready"
    return None


async def _run_job(request: JobRequest, steps: int) -> None:
    async with semaphore:
        execution_lease: ManagedGpuLease | None = None
        try:
            # Keep the durable job in submitted state while waiting for host
            # capacity.  No ByteFF2/OpenMM child exists before this succeeds.
            execution_lease = await runner.acquire_execution_lease(request.job_id)
            running_update_result = await _safe_update_status(
                request.job_id,
                "running",
                progress_percent=5,
                progress_stage="running",
                progress_message=(
                    f"Running ByteFF2 {request.protocol} formal protocol."
                    if request.run_mode == "formal"
                    else f"Running the {steps}-step ByteFF2 density demo."
                ),
            )
            if settings.db_configured and running_update_result is not JobUpdateResult.UPDATED:
                logger.warning("monomer MD job stopped before execution: %s", request.job_id)
                if running_update_result is None:
                    await _persist_terminal_status(
                        request.job_id,
                        "failed",
                        error="Monomer MD worker could not persist the running state.",
                        error_category="worker_status_update_failed",
                        progress_stage="failed",
                        progress_message="Monomer MD worker could not persist the running state.",
                    )
                await _release_execution_lease_safely(execution_lease, request.job_id)
                return
            result = await runner.run(
                request,
                steps,
                execution_lease=execution_lease,
            )
            await runner.release_execution_lease(execution_lease)
            execution_lease = None
        except asyncio.CancelledError:
            await _release_execution_lease_safely(execution_lease, request.job_id)
            await _persist_terminal_status(
                request.job_id,
                "failed",
                error="Monomer MD worker shut down before this job finished.",
                error_category="worker_shutdown",
                progress_stage="failed",
                progress_message="Monomer MD worker shut down before this job finished.",
            )
            raise
        except Exception as exc:
            await _release_execution_lease_safely(execution_lease, request.job_id)
            logger.exception("monomer MD job failed: %s", request.job_id)
            await _persist_terminal_status(
                request.job_id,
                "failed",
                error=str(exc),
                error_category=_classify_error(exc),
                progress_stage="failed",
                progress_message=str(exc)[:500],
            )
            return

        artifacts = {
            "artifact_root": str(result.output_dir),
            "outputs": result.result.get("outputs", {}),
        }
        if isinstance(result.result.get("artifacts"), dict):
            artifacts.update(result.result["artifacts"])
        artifact_manifest = result.result.get("artifact_manifest") if isinstance(result.result.get("artifact_manifest"), dict) else None
        result_summary = result.result.get("summary") if isinstance(result.result.get("summary"), dict) else None
        await _persist_terminal_status(
            request.job_id,
            "completed",
            result=result.result,
            output_dir=str(result.output_dir),
            artifacts=artifacts,
            completed_steps=result.completed_steps,
            progress_percent=100,
            progress_stage="completed",
            progress_message=(
                f"ByteFF2 {request.protocol} formal protocol completed."
                if request.run_mode == "formal"
                else "Density demo completed."
            ),
            artifact_manifest=artifact_manifest,
            result_summary=result_summary,
            byteff2_git_sha=result.result.get("byteff2_git_sha") if isinstance(result.result.get("byteff2_git_sha"), str) else None,
            gpu_device=result.result.get("gpu_device") if isinstance(result.result.get("gpu_device"), str) else None,
        )


async def _release_execution_lease_safely(
    execution_lease: ManagedGpuLease | None,
    job_id: str,
) -> None:
    if execution_lease is None:
        return
    if getattr(execution_lease, "termination_unsafe", False):
        execution_lease.abandon()
        return
    try:
        await runner.release_execution_lease(execution_lease)
    except Exception:
        # The reservation remains fail-closed in the Broker.  Logging retains
        # evidence and a Worker restart will eventually clear the exact owner.
        logger.exception("failed to release GPU execution lease for job %s", job_id)


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
    artifact_manifest: dict[str, Any] | None = None,
    result_summary: dict[str, Any] | None = None,
    byteff2_git_sha: str | None = None,
    gpu_device: str | None = None,
    error_category: str | None = None,
) -> JobUpdateResult | None:
    if not settings.db_configured:
        return JobUpdateResult.UPDATED
    try:
        update_result = await asyncio.to_thread(
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
            artifact_manifest=artifact_manifest,
            result_summary=result_summary,
            byteff2_git_sha=byteff2_git_sha,
            gpu_device=gpu_device,
            error_category=error_category,
            worker_instance_id=worker_instance_id,
        )
    except Exception:
        logger.exception("failed to update monomer MD job status: %s", job_id)
        return None
    if update_result is JobUpdateResult.ALREADY_TERMINAL:
        logger.info("monomer MD job already reached a terminal state: %s", job_id)
    elif update_result is JobUpdateResult.MISSING:
        logger.error("monomer MD job row is missing; releasing local capacity: %s", job_id)
    return update_result


async def _persist_terminal_status(job_id: str, status_value: str, **kwargs: Any) -> bool:
    while True:
        update_result = await _safe_update_status(job_id, status_value, **kwargs)
        if update_result in {
            JobUpdateResult.UPDATED,
            JobUpdateResult.ALREADY_TERMINAL,
            JobUpdateResult.MISSING,
        }:
            return True
        if shutting_down:
            return False
        await asyncio.sleep(5)


async def _attempt_recovery() -> bool:
    global recovery_ready
    if not settings.db_configured:
        recovery_ready = True
        return True
    try:
        recovered = await asyncio.to_thread(
            repository.reconcile_orphaned_jobs,
            worker_instance_id,
        )
    except Exception:
        recovery_ready = False
        logger.exception("failed to reconcile orphaned monomer MD jobs")
        return False
    recovery_ready = True
    if recovered:
        logger.warning("marked %s orphaned monomer MD job(s) failed", recovered)
    return True


async def _recovery_loop() -> None:
    while not recovery_ready:
        await asyncio.sleep(settings.recovery_retry_seconds)
        await _attempt_recovery()


async def _heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(settings.heartbeat_interval_seconds)
        job_ids = list(active_jobs)
        if not job_ids or not settings.db_configured:
            continue
        try:
            await asyncio.to_thread(
                repository.heartbeat,
                job_ids,
                worker_instance_id,
            )
        except Exception:
            logger.exception("failed to heartbeat active monomer MD jobs")


def _classify_error(exc: Exception) -> str:
    message = str(exc).lower()
    if isinstance(exc, GpuBrokerClientError):
        if exc.code == "gpu_capacity_unavailable":
            return "gpu_capacity_unavailable"
        if exc.code in {"gpu_lease_lost", "stale_fencing_token", "unknown_lease"}:
            return "gpu_lease_lost"
        return "gpu_runtime_unhealthy"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timed out" in message:
        return "timeout"
    if "not found" in message or "not importable" in message:
        return "runtime_missing"
    if "result file" in message or "required files" in message or "artifact" in message:
        return "artifact_missing"
    if "gmx" in message or "exit code" in message:
        return "byteff2_failed"
    return "worker_failed"
