from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status

from .config import WorkerSettings, load_settings
from .models import (
    ArtifactDeletionResponse,
    DrainResponse,
    HealthResponse,
    JobAccepted,
    JobCancellationResponse,
    JobRequest,
)
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

# The Worker also hardens its own process.  A service manager or developer
# shell is not a sufficient authority for files created later by background
# tasks.
os.umask(0o077)


_WORKER_MODULE_PATH = Path("workers/monomer_md_worker/app/main.py")
_DEV_VENV_NAME = ".venv-monomer-md-worker"
_DEV_LOCK_PATH = Path("workers/monomer_md_worker/requirements.lock")
_DEV_LOCK_RECORD = ".nexpoly-worker-lock-digest.json"
_DEV_BASE_RECORD = ".nexpoly-base-python-identity.json"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MD_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_STORAGE_TOMBSTONE_PREFIX = ".purge-"


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


def _owner_private_json(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 64 * 1024
        ):
            raise RuntimeError("development Worker identity record is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("development Worker identity record is unavailable") from exc
    if not isinstance(value, dict):
        raise RuntimeError("development Worker identity record is invalid")
    return value


def _development_git_identity(source_root: Path) -> tuple[str, str]:
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "LC_ALL": "C",
    }

    def run(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "--no-optional-locks", "-C", str(source_root), *arguments],
                env=environment,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("development Worker Git identity is unavailable") from exc
        return result.stdout.strip()

    top_level = run("rev-parse", "--show-toplevel")
    source_sha = run("rev-parse", "--verify", "HEAD")
    source_tree = run("rev-parse", "--verify", "HEAD^{tree}")
    if (
        Path(top_level).resolve(strict=True) != source_root
        or _SHA_RE.fullmatch(source_sha) is None
        or _SHA_RE.fullmatch(source_tree) is None
    ):
        raise RuntimeError("development Worker Git identity is invalid")
    return source_sha, source_tree


def _development_runtime_identity(
    source_root: Path,
    resolved_prefix: Path,
    resolved_executable: Path,
) -> tuple[str, str, str, str]:
    expected_prefix = source_root / _DEV_VENV_NAME
    try:
        expected_prefix = expected_prefix.resolve(strict=True)
        expected_executable = (expected_prefix / "bin/python").resolve(strict=True)
        lock = (source_root / _DEV_LOCK_PATH).resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("development Worker venv or lock is unavailable") from exc
    if resolved_prefix != expected_prefix or resolved_executable != expected_executable:
        raise RuntimeError("development Worker is not running from its isolated venv")
    if lock != source_root / _DEV_LOCK_PATH or lock.is_symlink() or not lock.is_file():
        raise RuntimeError("development Worker requirements lock is unsafe")

    lock_digest = "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
    lock_record = _owner_private_json(expected_prefix / _DEV_LOCK_RECORD)
    if lock_record != {
        "schema_version": 1,
        "path": _DEV_LOCK_PATH.as_posix(),
        "sha256": lock_digest,
    }:
        raise RuntimeError("development Worker lock identity has drifted")
    base_record = _owner_private_json(expected_prefix / _DEV_BASE_RECORD)
    base_identity = base_record.get("identity_sha256")
    if not isinstance(base_identity, str) or _DIGEST_RE.fullmatch(base_identity) is None:
        raise RuntimeError("development Worker base Python identity is invalid")
    material = {key: value for key, value in base_record.items() if key != "identity_sha256"}
    canonical_identity = "sha256:" + hashlib.sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    if canonical_identity != base_identity:
        raise RuntimeError("development Worker base Python identity has drifted")
    configured_executable = base_record.get("configured_path")
    recorded_executable = base_record.get("resolved_path")
    recorded_digest = base_record.get("executable_sha256")
    recorded_size = base_record.get("executable_size")
    if (
        not isinstance(configured_executable, str)
        or not isinstance(recorded_executable, str)
        or not isinstance(recorded_digest, str)
        or _DIGEST_RE.fullmatch(recorded_digest) is None
        or isinstance(recorded_size, bool)
        or not isinstance(recorded_size, int)
        or recorded_size <= 0
    ):
        raise RuntimeError("development Worker base Python executable is invalid")
    try:
        configured_path = Path(configured_executable)
        if not configured_path.is_absolute() or ".." in configured_path.parts:
            raise RuntimeError("development Worker base Python executable is invalid")
        frozen_executable = configured_path.resolve(strict=True)
        if frozen_executable != Path(recorded_executable):
            raise RuntimeError("development Worker base Python executable has drifted")
        metadata = frozen_executable.stat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o022
            or metadata.st_size != recorded_size
            or "sha256:" + hashlib.sha256(frozen_executable.read_bytes()).hexdigest()
            != recorded_digest
        ):
            raise RuntimeError("development Worker base Python executable has drifted")
    except OSError as exc:
        raise RuntimeError("development Worker base Python executable is unavailable") from exc
    source_sha, source_tree = _development_git_identity(source_root)
    return source_sha, source_tree, lock_digest, base_identity


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
    else:
        (
            source_sha,
            source_tree,
            worker_lock_sha256,
            base_python_identity_sha256,
        ) = _development_runtime_identity(
            source_root,
            resolved_prefix,
            resolved_executable,
        )

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
formal_job_queue: deque[str] = deque()
job_start_events: dict[str, asyncio.Event] = {}
cancel_requested_jobs: set[str] = set()
execution_job_id: str | None = None
active_jobs_lock = asyncio.Lock()
storage_cleanup_lock = asyncio.Lock()
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
BROKER_QUEUE_TRANSITION_WAIT_SECONDS = 12.0
BROKER_QUEUE_TRANSITION_POLL_SECONDS = 0.2


@asynccontextmanager
async def lifespan(_: FastAPI):
    global heartbeat_task, recovery_task, shutting_down, draining
    shutting_down = False
    draining = False
    await asyncio.to_thread(_recover_storage_tombstones)
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
    global execution_job_id
    steps = request.steps or settings.default_steps
    if request.run_mode == "demo" and steps > settings.max_steps:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"steps must be <= {settings.max_steps}",
        )
    health_response = await _wait_for_formal_queue_broker_transition(
        await _build_health_response(),
        request,
    )
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
        if request.run_mode == "formal" and len(active_jobs) != len(active_formal_jobs):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="formal ByteFF2 jobs cannot run while DensityDemo is active",
            )
        if request.run_mode == "formal" and len(active_formal_jobs) >= 3:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="formal ByteFF2 monomer MD capacity is full",
            )
        if request.run_mode == "demo" and active_jobs:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="DensityDemo cannot run while another monomer MD job is active",
            )

        queued = execution_job_id is not None
        try:
            initial_update_result = await asyncio.to_thread(
                repository.accept_job,
                request.job_id,
                worker_instance_id,
                queued=queued,
            )
        except Exception:
            logger.exception("failed to accept monomer MD job row: %s", request.job_id)
            initial_update_result = None
        if settings.db_configured and initial_update_result is not JobUpdateResult.UPDATED:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="monomer MD job row is not available for status updates",
            )

        start_event = asyncio.Event()
        job_start_events[request.job_id] = start_event
        if queued:
            formal_job_queue.append(request.job_id)
        else:
            execution_job_id = request.job_id
            start_event.set()
        task = asyncio.create_task(_run_job(request, steps))
        active_jobs[request.job_id] = task
        if request.run_mode == "formal":
            active_formal_jobs.add(request.job_id)
        task.add_done_callback(
            lambda _, job_id=request.job_id: asyncio.create_task(_remove_active_job(job_id))
        )
    return JobAccepted(
        job_id=request.job_id,
        status="submitted",
        mode=settings.mode,
        steps=steps,
        worker_id=settings.worker_id,
        worker_job_id=request.job_id,
        worker_version=settings.worker_version,
    )


async def _remove_active_job(job_id: str) -> None:
    global execution_job_id
    promote_job_id: str | None = None
    async with active_jobs_lock:
        active_jobs.pop(job_id, None)
        active_formal_jobs.discard(job_id)
        cancel_requested_jobs.discard(job_id)
        job_start_events.pop(job_id, None)
        try:
            formal_job_queue.remove(job_id)
        except ValueError:
            pass
        if execution_job_id == job_id:
            execution_job_id = None
            while formal_job_queue:
                candidate = formal_job_queue.popleft()
                if candidate in active_jobs:
                    execution_job_id = candidate
                    promote_job_id = candidate
                    break
    if promote_job_id is None:
        return
    try:
        promoted = await asyncio.to_thread(repository.promote_queued_job, promote_job_id)
    except Exception:
        logger.exception("failed to promote queued monomer MD job: %s", promote_job_id)
        promoted = None
    if settings.db_configured and promoted is not JobUpdateResult.UPDATED:
        task = active_jobs.get(promote_job_id)
        if task is not None:
            task.cancel()
        return
    event = job_start_events.get(promote_job_id)
    if event is not None:
        event.set()


@app.post("/jobs/{job_id}/cancel", response_model=JobCancellationResponse)
async def cancel_job(job_id: str) -> JobCancellationResponse:
    async with active_jobs_lock:
        task = active_jobs.get(job_id)
        if task is not None:
            cancel_requested_jobs.add(job_id)
            task.cancel()
            return JobCancellationResponse(
                job_id=job_id,
                status="cancel_requested",
                message="Cancellation is being processed by the monomer MD worker.",
            )

    update_result = await _safe_update_status(
        job_id,
        "cancelled",
        progress_stage="cancelled",
        progress_message="Monomer MD cancellation completed.",
    )
    if update_result is JobUpdateResult.MISSING:
        raise HTTPException(status_code=404, detail="monomer MD job is not active on this worker")
    return JobCancellationResponse(
        job_id=job_id,
        status="cancelled",
        message="Monomer MD cancellation has completed.",
    )


@app.delete("/jobs/{job_id}/artifacts", response_model=ArtifactDeletionResponse)
async def delete_job_artifacts(job_id: str) -> ArtifactDeletionResponse:
    if _MD_JOB_ID_RE.fullmatch(job_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="monomer MD job storage was not found",
        )
    artifact_root = runner.output_dir_for_job(job_id)
    tombstone = artifact_root.parent / f"{_STORAGE_TOMBSTONE_PREFIX}{job_id}"
    try:
        async with storage_cleanup_lock:
            async with active_jobs_lock:
                if job_id in active_jobs or job_id in cancel_requested_jobs:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="cannot delete artifacts for an active monomer MD job",
                    )
                detached = await asyncio.to_thread(
                    _detach_artifact_entry,
                    artifact_root,
                    tombstone,
                )
            removed = await asyncio.to_thread(
                _durably_remove_artifact_entry,
                tombstone,
            )
    except HTTPException:
        raise
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="monomer MD storage cleanup could not prove absence",
        ) from exc
    deleted = detached or removed
    if deleted:
        return ArtifactDeletionResponse(
            job_id=job_id,
            deleted=True,
            storage_state="absent",
            artifact_root=str(artifact_root),
            message="artifacts deleted",
        )
    return ArtifactDeletionResponse(
        job_id=job_id,
        deleted=False,
        storage_state="absent",
        artifact_root=str(artifact_root),
        message="artifacts were already absent",
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durably_remove_artifact_entry(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        _fsync_directory(path.parent)
        return False
    if stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()
    _fsync_directory(path.parent)
    return True


def _detach_artifact_entry(path: Path, tombstone: Path) -> bool:
    for candidate in (path, tombstone):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError("unsafe monomer MD storage deletion path")
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("monomer MD storage path is not a directory")
    path_exists = os.path.lexists(path)
    tombstone_exists = os.path.lexists(tombstone)
    if path_exists and tombstone_exists:
        raise RuntimeError(
            "monomer MD storage and deletion tombstone coexist"
        )
    if not path_exists:
        _fsync_directory(path.parent)
        return False
    os.rename(path, tombstone)
    _fsync_directory(path.parent)
    return True


def _recover_storage_tombstones() -> None:
    root = settings.job_root
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("monomer MD job root is unsafe")
    changed = False
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if not child.name.startswith(_STORAGE_TOMBSTONE_PREFIX):
            continue
        job_id = child.name[len(_STORAGE_TOMBSTONE_PREFIX) :]
        if _MD_JOB_ID_RE.fullmatch(job_id) is None:
            raise RuntimeError("unsafe monomer MD purge tombstone name")
        canonical = root / job_id
        if os.path.lexists(canonical):
            raise RuntimeError(
                "monomer MD storage and deletion tombstone coexist"
            )
        metadata = child.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError("unsafe monomer MD purge tombstone")
        shutil.rmtree(child)
        changed = True
    if changed:
        _fsync_directory(root)


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


def _formal_queue_broker_transition(
    health_response: HealthResponse,
    request: JobRequest,
) -> bool:
    error = health_response.gpu_broker_error or ""
    return (
        request.run_mode == "formal"
        and execution_job_id is not None
        and health_response.mode == "real"
        and health_response.db_configured
        and health_response.byteff2_root_exists
        and health_response.runtime_ready
        and health_response.gpu_broker_enabled
        and not health_response.gpu_broker_ready
        and not health_response.draining
        and not getattr(runner, "gpu_admission_uncertain", False)
        and 0 < health_response.active_jobs < health_response.max_active_jobs
        and error.startswith(
            (
                "broker_timeout:",
                "gpu_broker_unavailable:",
                "broker_unavailable:",
            )
        )
    )


async def _wait_for_formal_queue_broker_transition(
    health_response: HealthResponse,
    request: JobRequest,
) -> HealthResponse:
    if not _formal_queue_broker_transition(health_response, request):
        return health_response
    deadline = (
        asyncio.get_running_loop().time() + BROKER_QUEUE_TRANSITION_WAIT_SECONDS
    )
    current = health_response
    while _formal_queue_broker_transition(current, request):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(BROKER_QUEUE_TRANSITION_POLL_SECONDS, remaining))
        current = await _build_health_response()
    return current


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
    start_event = job_start_events[request.job_id]
    try:
        await start_event.wait()
    except asyncio.CancelledError:
        user_cancelled = request.job_id in cancel_requested_jobs
        await _persist_terminal_status(
            request.job_id,
            "cancelled" if user_cancelled else "failed",
            error=None if user_cancelled else "Monomer MD worker shut down before this queued job started.",
            error_category=None if user_cancelled else "worker_shutdown",
            progress_stage="cancelled" if user_cancelled else "failed",
            progress_message=(
                "Queued monomer MD job was cancelled."
                if user_cancelled
                else "Monomer MD worker shut down before this queued job started."
            ),
        )
        raise
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
            cleanup_safe = await _release_execution_lease_safely(
                execution_lease,
                request.job_id,
            )
            user_cancelled = request.job_id in cancel_requested_jobs
            cancellation_completed = user_cancelled and cleanup_safe
            cleanup_error = (
                "Monomer MD cancellation could not prove safe GPU resource cleanup; "
                "capacity remains fail-closed."
            )
            await _persist_terminal_status(
                request.job_id,
                "cancelled" if cancellation_completed else "failed",
                error=(
                    None
                    if cancellation_completed
                    else cleanup_error
                    if user_cancelled
                    else "Monomer MD worker shut down before this job finished."
                ),
                error_category=(
                    None
                    if cancellation_completed
                    else "cancel_cleanup_unconfirmed"
                    if user_cancelled
                    else "worker_shutdown"
                ),
                progress_stage="cancelled" if cancellation_completed else "failed",
                progress_message=(
                    "Monomer MD job was cancelled and its execution resources were released."
                    if cancellation_completed
                    else cleanup_error
                    if user_cancelled
                    else "Monomer MD worker shut down before this job finished."
                ),
                allow_cancel_requested_failure=user_cancelled and not cleanup_safe,
            )
            raise
        except Exception as exc:
            cleanup_safe = await _release_execution_lease_safely(
                execution_lease,
                request.job_id,
            )
            user_cancelled = request.job_id in cancel_requested_jobs
            logger.exception("monomer MD job failed: %s", request.job_id)
            await _persist_terminal_status(
                request.job_id,
                "failed",
                error=str(exc),
                error_category=(
                    "cancel_cleanup_failed"
                    if user_cancelled
                    else _classify_error(exc)
                ),
                progress_stage="failed",
                progress_message=str(exc)[:500],
                allow_cancel_requested_failure=user_cancelled or not cleanup_safe,
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
        try:
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
        except asyncio.CancelledError:
            user_cancelled = request.job_id in cancel_requested_jobs
            await _persist_terminal_status(
                request.job_id,
                "cancelled" if user_cancelled else "failed",
                error=None if user_cancelled else "Monomer MD worker shut down before completion was persisted.",
                error_category=None if user_cancelled else "worker_shutdown",
                progress_stage="cancelled" if user_cancelled else "failed",
                progress_message=(
                    "Monomer MD cancellation completed after execution resources were released."
                    if user_cancelled
                    else "Monomer MD worker shut down before completion was persisted."
                ),
            )
            raise


async def _release_execution_lease_safely(
    execution_lease: ManagedGpuLease | None,
    job_id: str,
) -> bool:
    if execution_lease is None:
        return not getattr(runner, "gpu_admission_uncertain", False)
    if getattr(execution_lease, "termination_unsafe", False):
        execution_lease.abandon()
        return False
    try:
        await runner.release_execution_lease(execution_lease)
    except Exception:
        # The reservation remains fail-closed in the Broker.  Logging retains
        # evidence and a Worker restart will eventually clear the exact owner.
        logger.exception("failed to release GPU execution lease for job %s", job_id)
        return False
    return not getattr(execution_lease, "termination_unsafe", False)


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
    allow_cancel_requested_failure: bool = False,
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
            allow_cancel_requested_failure=allow_cancel_requested_failure,
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
        if update_result is JobUpdateResult.CANCEL_REQUESTED:
            cancelled = await _safe_update_status(
                job_id,
                "cancelled",
                progress_stage="cancelled",
                progress_message="Monomer MD cancellation completed after safe resource cleanup.",
            )
            return cancelled in {
                JobUpdateResult.UPDATED,
                JobUpdateResult.ALREADY_TERMINAL,
            }
        if shutting_down:
            return False
        await asyncio.sleep(5)


async def _attempt_recovery() -> bool:
    global recovery_ready
    if not settings.db_configured:
        recovery_ready = True
        return True
    try:
        cancelled = await asyncio.to_thread(
            repository.reconcile_cancel_requested_jobs,
        )
        recovered = await asyncio.to_thread(
            repository.reconcile_orphaned_jobs,
            worker_instance_id,
        )
    except Exception:
        recovery_ready = False
        logger.exception("failed to reconcile orphaned monomer MD jobs")
        return False
    recovery_ready = True
    if cancelled:
        logger.warning("marked %s orphaned cancellation request(s) cancelled", cancelled)
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
