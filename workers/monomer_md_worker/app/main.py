from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
import json
import re
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status

from .config import WorkerSettings, load_settings
from .formal_protocols import FORMAL_PROTOCOLS
from .models import ArtifactDeletionResponse, DrainResponse, HealthResponse, JobAccepted, JobRequest
from .repository import JobUpdateResult, PostgresJobRepository
from .runner import MonomerMdRunner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("monomer_md_worker")


_SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
_WORKER_MODULE_PATH = Path("workers/monomer_md_worker/app/main.py")


@dataclass(frozen=True)
class RuntimeIdentity:
    source_sha: str | None
    source_root: str
    venv_prefix: str
    python_executable: str


def _load_runtime_identity(
    *,
    module_path: Path | None = None,
    python_prefix: Path | None = None,
    python_executable: Path | None = None,
) -> RuntimeIdentity:
    """Derive release identity from loaded code, never from mutable environment."""

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
    # Path(__file__).resolve() traverses ops/current and lands in this layout.
    # Treat every source below an ops/releases tree as production and fail
    # closed when either the directory, manifest, or release venv is stale.
    is_release_source = (
        source_root.parent.name == "releases"
        and source_root.parent.parent.name == "ops"
    )
    if is_release_source:
        if _SOURCE_SHA_RE.fullmatch(source_root.name) is None:
            raise RuntimeError("production Worker release directory is not a full source SHA")
        source_sha = source_root.name
        manifest_path = source_root / "release-manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise RuntimeError("production Worker release manifest is missing or unsafe")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("production Worker release manifest is unreadable") from exc
        if not isinstance(manifest, dict) or manifest.get("source_sha") != source_sha:
            raise RuntimeError("production Worker source SHA differs from its release manifest")

        expected_prefix_path = source_root / "worker-venv"
        if expected_prefix_path.is_symlink():
            raise RuntimeError("production Worker release venv must not be a symlink")
        try:
            expected_prefix = expected_prefix_path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("production Worker release venv is missing") from exc
        if resolved_prefix != expected_prefix:
            raise RuntimeError("production Worker is not running from its release venv")

    return RuntimeIdentity(
        source_sha=source_sha,
        source_root=str(source_root),
        venv_prefix=str(resolved_prefix),
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


@asynccontextmanager
async def lifespan(_: FastAPI):
    global heartbeat_task, recovery_task, shutting_down, draining
    shutting_down = False
    draining = False
    if settings.db_configured:
        await _attempt_recovery()
        if not recovery_ready:
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
    return _build_health_response()


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
        accepting_jobs=_build_health_response().accepting_jobs,
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
    health_response = _build_health_response()
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


def _build_health_response() -> HealthResponse:
    byteff2_root_exists = settings.byteff2_root.exists()
    runtime_ready = True
    runtime_error = None
    protocols: dict[str, Any] = {}
    if settings.mode == "real":
        if byteff2_root_exists:
            probe_result = _probe_real_runtime()
            if len(probe_result) == 2:
                runtime_ready, runtime_error = probe_result
                protocols = {}
            else:
                runtime_ready, runtime_error, protocols = probe_result
        else:
            runtime_ready = False
            runtime_error = f"ByteFF2 root does not exist: {settings.byteff2_root}"
            protocols = _unready_protocols(runtime_error)

    worker_status = "ok"
    if settings.mode == "real" and (
        not byteff2_root_exists or not settings.db_configured or not runtime_ready
    ):
        worker_status = "degraded"
    if settings.db_configured and not recovery_ready:
        worker_status = "degraded"
        runtime_error = runtime_error or "worker database recovery has not completed"
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
        source_root=runtime_identity.source_root,
        venv_prefix=runtime_identity.venv_prefix,
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
    )


def _probe_real_runtime() -> tuple[bool, str | None, dict[str, Any]]:
    demo_entry_error = _configured_density_demo_entry_error()
    if demo_entry_error is not None:
        return False, demo_entry_error, _unready_protocols(demo_entry_error)

    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = settings.cuda_visible_devices
    env["PYTHONPATH"] = os.pathsep.join(
        [str(settings.byteff2_root), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    try:
        demo_completed = subprocess.run(
            [
                settings.byteff2_python,
                "-c",
                (
                    "import openmm as omm; import MDAnalysis; import pandas; "
                    "import byteff2.toolkit.protocol as p; "
                    "from MDAnalysis.lib.formats.libdcd import DCDFile; "
                    "omm.Platform.getPlatformByName('CUDA'); "
                    "assert hasattr(p, 'DensityProtocol'), 'DensityProtocol is not available'; "
                    "print('demo runtime ready')"
                ),
            ],
            cwd=settings.byteff2_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=settings.health_probe_timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        error = f"BYTEFF2_PYTHON not found: {settings.byteff2_python}"
        return False, error, _unready_protocols(error)
    except subprocess.TimeoutExpired:
        error = f"runtime import probe timed out after {settings.health_probe_timeout_seconds}s"
        return False, error, _unready_protocols(error)
    except OSError as exc:
        error = str(exc)
        return False, error, _unready_protocols(error)

    if demo_completed.returncode != 0:
        error = _completed_process_error(demo_completed, "runtime import probe")
        return False, error, _unready_protocols(error)

    try:
        completed = subprocess.run(
            [
                settings.byteff2_python,
                "-c",
                (
                    "import json; "
                    "import byteff2.toolkit.protocol as p; "
                    "protocols=['Density','Transport','HVap','Dielectric','Compressibility']; "
                    "data={name:{'supported':hasattr(p, name+'Protocol')} for name in protocols}\n"
                    "try:\n import velocityverletplugin; data['Transport']['velocityverletplugin']=True\n"
                    "except Exception as exc:\n data['Transport']['velocityverletplugin']=False; data['Transport']['velocityverletplugin_error']=str(exc)\n"
                    "print(json.dumps(data))"
                ),
            ],
            cwd=settings.byteff2_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=settings.health_probe_timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        error = f"BYTEFF2_PYTHON not found: {settings.byteff2_python}"
        return True, None, _unready_protocols(error)
    except subprocess.TimeoutExpired:
        error = f"formal protocol import probe timed out after {settings.health_probe_timeout_seconds}s"
        return True, None, _unready_protocols(error)
    except OSError as exc:
        error = str(exc)
        return True, None, _unready_protocols(error)

    if completed.returncode != 0:
        error = _completed_process_error(completed, "formal protocol import probe")
        return True, None, _unready_protocols(error)
    protocol_probe = _parse_protocol_probe(completed.stdout)

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
        error = "gmx was not found on PATH"
        return False, error, _protocols_with_runtime_error(protocol_probe, error)
    except subprocess.TimeoutExpired:
        error = f"gmx probe timed out after {settings.health_probe_timeout_seconds}s"
        return False, error, _protocols_with_runtime_error(protocol_probe, error)
    except OSError as exc:
        error = str(exc)
        return False, error, _protocols_with_runtime_error(protocol_probe, error)

    if gmx_completed.returncode == 0:
        return True, None, _protocol_health_from_probe(protocol_probe)
    error = _completed_process_error(gmx_completed, "gmx probe")
    return False, error, _protocols_with_runtime_error(protocol_probe, error)


def _completed_process_error(completed: subprocess.CompletedProcess[str], label: str) -> str:
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    message = detail[-1] if detail else f"{label} exited {completed.returncode}"
    return message[:500]


def _parse_protocol_probe(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        return data if isinstance(data, dict) else {}
    return {}


def _protocol_health_from_probe(probe: dict[str, Any]) -> dict[str, Any]:
    health: dict[str, Any] = {}
    for protocol in FORMAL_PROTOCOLS:
        item = probe.get(protocol) if isinstance(probe.get(protocol), dict) else {}
        supported = item.get("supported") is True
        runtime_ready = supported
        runtime_error = None
        if protocol == "Transport" and item.get("velocityverletplugin") is not True:
            runtime_ready = False
            runtime_error = item.get("velocityverletplugin_error") or "velocityverletplugin is not importable"
        health[protocol] = {
            "protocol": protocol,
            "run_mode": "formal",
            "supported": supported,
            "runtime_ready": runtime_ready,
            "runtime_error": runtime_error,
        }
    return health


def _unready_protocols(error: str) -> dict[str, Any]:
    return {
        protocol: {
            "protocol": protocol,
            "run_mode": "formal",
            "supported": False,
            "runtime_ready": False,
            "runtime_error": error,
        }
        for protocol in FORMAL_PROTOCOLS
    }


def _protocols_with_runtime_error(probe: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        protocol: {
            "protocol": protocol,
            "run_mode": "formal",
            "supported": (
                isinstance(probe.get(protocol), dict)
                and probe[protocol].get("supported") is True
            ),
            "runtime_ready": False,
            "runtime_error": error,
        }
        for protocol in FORMAL_PROTOCOLS
    }


def _configured_density_demo_entry_error() -> str | None:
    configured = os.getenv("BYTEFF2_DENSITY_DEMO_ENTRY", "").strip()
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        path = settings.byteff2_root / path
    if path.exists():
        return None
    return f"BYTEFF2_DENSITY_DEMO_ENTRY does not exist: {path}"


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
            return
        try:
            result = await runner.run(request, steps)
        except asyncio.CancelledError:
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
    if "timed out" in message:
        return "timeout"
    if "not found" in message or "not importable" in message:
        return "runtime_missing"
    if "result file" in message or "required files" in message or "artifact" in message:
        return "artifact_missing"
    if "gmx" in message or "exit code" in message:
        return "byteff2_failed"
    return "worker_failed"
