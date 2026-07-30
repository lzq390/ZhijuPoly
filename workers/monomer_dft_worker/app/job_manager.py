from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import re
import shutil
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Literal

from .artifacts import (
    BUNDLE_CREATING_NAME,
    atomic_write_json,
    build_bundle,
    ensure_private_directory,
    open_verified_artifact,
    sha256_open_file,
)
from .chemistry import (
    MODEL_DOMAINS,
    ChemistryValidationError,
    validate_request_chemistry,
)
from .engine import (
    ComputationCancelled,
    EngineExecution,
    ScientificComputationError,
    ScientificEngine,
)
from .journal_upgrade import JobRootLock, JournalUpgradeError
from .schemas import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    ArtifactDeletionResponse,
    ArtifactDescriptor,
    ArtifactState,
    DrainResponse,
    EnqueueSequenceSource,
    JobJournalV2,
    JobListResponse,
    JobPurgeRequest,
    JobPurgeResponse,
    JobSnapshot,
    JobSubmitRequest,
    PublicJobSnapshot,
    StructuredError,
    default_job_timings,
    validate_artifact_name,
)


SINGLE_POINT_TIMEOUT_SECONDS = 600.0
OPTIMIZATION_TIMEOUT_SECONDS = 1800.0
_UNSET = object()
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PURGE_TOMBSTONE_PREFIX = ".purge-"


class JobManagerError(RuntimeError):
    status_code = 500
    code = "job_manager_error"


class JobNotFound(JobManagerError):
    status_code = 404
    code = "job_not_found"


class JobConflict(JobManagerError):
    status_code = 409
    code = "job_conflict"


class QueueFull(JobManagerError):
    status_code = 429
    code = "queue_full"


class WorkerUnavailable(JobManagerError):
    status_code = 503
    code = "worker_unavailable"


class JournalPersistenceError(WorkerUnavailable):
    code = "journal_persistence_failed"


class ArtifactNotFound(JobManagerError):
    status_code = 404
    code = "artifact_not_found"


class ArtifactDeletionFailed(WorkerUnavailable):
    code = "artifact_deletion_failed"


@dataclass(slots=True)
class ArtifactAccess:
    descriptor: ArtifactDescriptor
    path: Path
    stream: BinaryIO


@dataclass(slots=True)
class BundleAccess:
    path: Path
    stream: BinaryIO
    size_bytes: int
    sha256: str


@dataclass(slots=True)
class _JobRecord:
    snapshot: JobSnapshot
    enqueue_sequence: int
    enqueue_sequence_source: EnqueueSequenceSource
    artifact_state: ArtifactState = "none"
    artifact_manifest: tuple[ArtifactDescriptor, ...] = ()
    artifact_delete_requested_at: datetime | None = None
    artifacts_deleted_at: datetime | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    artifact_io_lock: threading.Lock = field(default_factory=threading.Lock)
    gpu_wait_started_at: datetime | None = None
    dispatch_queue_wait_ms: float = 0.0
    admission_gpu_wait_ms: float = 0.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobManager:
    """Durable FIFO scheduler for one GPU calculation at a time."""

    def __init__(
        self,
        *,
        job_root: Path,
        engine: ScientificEngine,
        runtime: Any,
        worker_version: str,
        max_queued_jobs: int = 8,
        fatal_exit: Callable[[], None] | None = None,
        journal_writer: Callable[[Path, Any], None] = atomic_write_json,
        single_point_timeout_seconds: float = SINGLE_POINT_TIMEOUT_SECONDS,
        optimization_timeout_seconds: float = OPTIMIZATION_TIMEOUT_SECONDS,
    ) -> None:
        if max_queued_jobs != 8:
            raise ValueError("the monomer DFT worker queue capacity must be exactly 8")
        self.job_root = Path(job_root)
        self.engine = engine
        self.runtime = runtime
        self.worker_version = worker_version
        self.max_queued_jobs = max_queued_jobs
        self.worker_instance_id = uuid.uuid4().hex
        self._fatal_exit = fatal_exit
        self._fatal_exit_scheduled = False
        self._journal_writer = journal_writer
        self.single_point_timeout_seconds = float(single_point_timeout_seconds)
        self.optimization_timeout_seconds = float(optimization_timeout_seconds)
        self._records: dict[str, _JobRecord] = {}
        self._sequence_to_job: dict[int, str] = {}
        self._purging_job_ids: set[str] = set()
        self._queue: deque[str] = deque()
        self._running_job_id: str | None = None
        self._draining = False
        self._recovering = False
        self._fatal = False
        self._fatal_reason: str | None = None
        self._shutdown = False
        self._state_lock = threading.RLock()
        self._wake = asyncio.Event()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._job_root_lock = JobRootLock(self.job_root)

    async def start(self) -> None:
        ensure_private_directory(self.job_root)
        self._event_loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._dispatcher_task is not None:
                return
            self._recovering = True
            self._shutdown = False
        try:
            self._job_root_lock.acquire()
            await asyncio.to_thread(self._load_journals)
        except JournalUpgradeError as exc:
            self._job_root_lock.release()
            with self._state_lock:
                self._recovering = False
            raise RuntimeError(str(exc)) from exc
        except BaseException:
            self._job_root_lock.release()
            with self._state_lock:
                self._recovering = False
            raise
        with self._state_lock:
            self._recovering = False
        self._dispatcher_task = asyncio.create_task(
            self._dispatch_loop(), name="monomer-dft-fifo-dispatcher"
        )
        if self._queue:
            self._wake.set()

    async def stop(self) -> None:
        try:
            with self._state_lock:
                self._draining = True
                self._shutdown = True
                running = self._record_or_none(self._running_job_id)
                if running is not None:
                    if running.snapshot.status == "running":
                        with contextlib.suppress(JournalPersistenceError):
                            self._update_snapshot(
                                running,
                                status="cancel_requested",
                            )
                        running.cancel_event.set()
            self._wake.set()
            task = self._dispatcher_task
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._dispatcher_task = None
            self._event_loop = None
        finally:
            self._job_root_lock.release()

    def _load_journals(self) -> None:
        # Startup recovery is rebuilt from durable state so a failed startup can
        # be retried safely on the same manager instance after remediation.
        self._records.clear()
        self._sequence_to_job.clear()
        self._purging_job_ids.clear()
        self._queue.clear()
        self._recover_purge_tombstones()
        candidates: list[_JobRecord] = []
        for child in sorted(self.job_root.iterdir(), key=lambda path: path.name):
            if child.name.startswith("."):
                continue
            if child.is_symlink() or not child.is_dir():
                raise RuntimeError(
                    f"unsafe entry below monomer DFT job root: {child.name}"
                )
            journals: list[Path] = []
            for attempt in child.iterdir():
                if attempt.name.startswith("."):
                    continue
                if attempt.is_symlink() or not attempt.is_dir():
                    raise RuntimeError(f"unsafe attempt entry below job {child.name}")
                journal = attempt / "journal.json"
                if journal.is_symlink():
                    raise RuntimeError(f"unsafe journal below job {child.name}")
                if journal.is_file():
                    journals.append(journal)
            if not journals:
                continue
            if len(journals) != 1:
                raise RuntimeError(
                    f"job {child.name} has multiple durable attempt journals"
                )
            journal = journals[0]
            try:
                raw_bytes = journal.read_bytes()
                raw_value = json.loads(raw_bytes)
                if not isinstance(raw_value, dict):
                    raise ValueError("journal root is not an object")
                if raw_value.get("journal_schema_version") != 2:
                    raise RuntimeError(
                        "legacy V1 journal requires the offline journal_upgrade tool"
                    )
                envelope = JobJournalV2.model_validate_json(raw_bytes)
            except Exception as exc:
                raise RuntimeError(f"invalid job journal: {child.name}") from exc
            snapshot = envelope.snapshot
            if (
                snapshot.job_id != child.name
                or snapshot.attempt_token != journal.parent.name
                or snapshot.request.job_id != snapshot.job_id
                or snapshot.request.attempt_token != snapshot.attempt_token
                or snapshot.request.request_sha256 != snapshot.request_sha256
            ):
                raise RuntimeError(f"inconsistent job journal identity: {child.name}")
            if envelope.enqueue_sequence in self._sequence_to_job:
                raise RuntimeError("duplicate enqueue_sequence across durable journals")
            record = _JobRecord(
                snapshot=snapshot,
                enqueue_sequence=envelope.enqueue_sequence,
                enqueue_sequence_source=envelope.enqueue_sequence_source,
                artifact_state=envelope.artifact_state,
                artifact_manifest=tuple(envelope.artifact_manifest),
                artifact_delete_requested_at=envelope.artifact_delete_requested_at,
                artifacts_deleted_at=envelope.artifacts_deleted_at,
            )
            self._recover_bundle_creation(snapshot)
            normalized_timings = default_job_timings()
            normalized_timings.update(snapshot.timings)
            snapshot.timings = normalized_timings
            self._records[snapshot.job_id] = record
            self._sequence_to_job[envelope.enqueue_sequence] = snapshot.job_id
            if snapshot.status in {"pending", "queued"}:
                candidates.append(record)
            elif snapshot.status in {"running", "cancel_requested"}:
                self._cleanup_partial_artifacts(snapshot)
                self._update_snapshot(
                    record,
                    status="failed",
                    stage="validating",
                    progress_percent=snapshot.progress_percent,
                    finished_at=_utcnow(),
                    artifacts=[],
                    artifact_state="none",
                    artifact_manifest=(),
                    error=StructuredError(
                        code="worker_restarted",
                        message="The worker stopped while this calculation was running.",
                        retryable=True,
                    ),
                )
            elif snapshot.status in {"failed", "cancelled"}:
                self._cleanup_partial_artifacts(snapshot)
                self._update_snapshot(
                    record,
                    artifacts=[],
                    artifact_state="none",
                    artifact_manifest=(),
                )
            elif record.artifact_state == "deleting":
                self._delete_artifact_files(record)
                self._update_snapshot(
                    record,
                    artifacts=[],
                    artifact_state="deleted",
                    artifacts_deleted_at=_utcnow(),
                )

        for record in sorted(candidates, key=lambda item: item.enqueue_sequence):
            if len(self._queue) >= self.max_queued_jobs:
                self._update_snapshot(
                    record,
                    status="failed",
                    stage="validating",
                    finished_at=_utcnow(),
                    error=StructuredError(
                        code="recovery_queue_overflow",
                        message="Recovered queue exceeds the configured capacity.",
                        retryable=True,
                    ),
                )
                continue
            self._update_snapshot(
                record,
                worker_instance_id=self.worker_instance_id,
                status="queued",
                stage="queued",
            )
            self._queue.append(record.snapshot.job_id)

    def validate_submission(self, request: JobSubmitRequest) -> None:
        validate_request_chemistry(
            request.input,
            request.model,
            requires_hessian="hessian" in request.properties,
        )

    def replay_submission(self, request: JobSubmitRequest) -> PublicJobSnapshot | None:
        """Return an existing identical job before any scientific revalidation."""
        with self._state_lock:
            return self._existing_submission_locked(request)

    def _existing_submission_locked(
        self,
        request: JobSubmitRequest,
    ) -> PublicJobSnapshot | None:
        if request.job_id in self._purging_job_ids:
            raise JobConflict("job deletion is in progress")
        existing = self._records.get(request.job_id)
        if existing is None:
            return None
        if existing.enqueue_sequence_source != "backend":
            raise JobConflict(
                "job_id belongs to a migrated legacy journal; a new backend "
                "enqueue sequence cannot replace that durable identity"
            )
        same_attempt = existing.snapshot.attempt_token == request.attempt_token
        same_request = existing.snapshot.request_sha256 == request.request_sha256
        same_sequence = existing.enqueue_sequence == request.enqueue_sequence
        if same_attempt and same_request and same_sequence:
            return self._public_snapshot(existing)
        raise JobConflict(
            "job_id already exists with a different attempt, request hash, or sequence"
        )

    def submit(
        self,
        request: JobSubmitRequest,
        *,
        chemistry_validated: bool = False,
    ) -> tuple[PublicJobSnapshot, bool]:
        replay = self.replay_submission(request)
        if replay is not None:
            return replay, False
        if not chemistry_validated:
            self.validate_submission(request)
        with self._state_lock:
            replay = self._existing_submission_locked(request)
            if replay is not None:
                return replay, False
            sequence_owner = self._sequence_to_job.get(request.enqueue_sequence)
            if sequence_owner is not None:
                raise JobConflict(
                    "enqueue_sequence already belongs to another durable job"
                )
            if self._draining or self._recovering or self._fatal or self._shutdown:
                raise WorkerUnavailable("worker is not accepting new jobs")
            probe = self.runtime.probe()
            if not bool(probe.ready):
                raise WorkerUnavailable("AIMNet runtime is not ready")
            if len(self._queue) >= self.max_queued_jobs:
                raise QueueFull("the monomer DFT worker queue is full")

            now = _utcnow()
            snapshot = JobSnapshot(
                enqueue_sequence=request.enqueue_sequence,
                job_id=request.job_id,
                attempt_token=request.attempt_token,
                request_sha256=request.request_sha256 or "",
                worker_instance_id=self.worker_instance_id,
                status="queued",
                stage="queued",
                progress_percent=0,
                created_at=now,
                updated_at=now,
                request=request,
            )
            record = _JobRecord(
                snapshot=snapshot,
                enqueue_sequence=request.enqueue_sequence,
                enqueue_sequence_source="backend",
            )
            self._persist_record(record)
            self._records[request.job_id] = record
            self._sequence_to_job[request.enqueue_sequence] = request.job_id
            self._queue.append(request.job_id)
            self._queue = deque(
                sorted(
                    self._queue,
                    key=lambda job_id: self._records[job_id].enqueue_sequence,
                )
            )
            self._wake.set()
            return self._public_snapshot(record), True

    def get(self, job_id: str) -> PublicJobSnapshot:
        with self._state_lock:
            return self._public_snapshot(self._record(job_id))

    def list(self, state: Literal["active", "all"] = "all") -> JobListResponse:
        with self._state_lock:
            records = sorted(
                self._records.values(), key=lambda record: record.enqueue_sequence
            )
            if state == "active":
                records = [
                    record
                    for record in records
                    if record.snapshot.status in ACTIVE_STATUSES
                ]
            jobs = [self._public_snapshot(record) for record in records]
            return JobListResponse(jobs=jobs, total=len(jobs))

    def cancel(
        self,
        job_id: str,
        request: JobSubmitRequest | None = None,
    ) -> PublicJobSnapshot:
        """Cancel a known job or durably fence an unknown dispatch claim.

        A Backend may commit a dispatch claim and crash before the Worker
        receives the corresponding submit.  Supplying the exact V2 submit
        envelope lets cancellation win that race without manufacturing an
        unverifiable scientific request from a hash alone.  The resulting
        standard cancelled Journal V2 is replayed by a late identical submit.

        The body-less form remains the legacy known-job operation: an unknown
        job still returns 404 and cannot create durable state.
        """
        with self._state_lock:
            if job_id in self._purging_job_ids:
                raise JobConflict("job deletion is in progress")
            record = self._records.get(job_id)
            if request is not None:
                if request.job_id != job_id:
                    raise JobConflict(
                        "cancel path job_id differs from the fenced request identity"
                    )
                if record is None:
                    sequence_owner = self._sequence_to_job.get(
                        request.enqueue_sequence
                    )
                    if sequence_owner is not None:
                        raise JobConflict(
                            "enqueue_sequence already belongs to another durable job"
                        )
                    now = _utcnow()
                    snapshot = JobSnapshot(
                        enqueue_sequence=request.enqueue_sequence,
                        job_id=request.job_id,
                        attempt_token=request.attempt_token,
                        request_sha256=request.request_sha256 or "",
                        worker_instance_id=self.worker_instance_id,
                        status="cancelled",
                        stage="queued",
                        progress_percent=0,
                        created_at=now,
                        updated_at=now,
                        finished_at=now,
                        request=request,
                    )
                    record = _JobRecord(
                        snapshot=snapshot,
                        enqueue_sequence=request.enqueue_sequence,
                        enqueue_sequence_source="backend",
                    )
                    # Persist first.  A crash after this atomic write is
                    # recovered as cancelled; a crash before it leaves no
                    # partially visible in-memory identity.
                    self._persist_record(record)
                    self._records[job_id] = record
                    self._sequence_to_job[request.enqueue_sequence] = job_id
                    record.cancel_event.set()
                    return self._public_snapshot(record)
                # Reuse submit's exact identity/scientific-payload fencing.
                self._existing_submission_locked(request)
            elif record is None:
                raise JobNotFound("unknown job_id")

            assert record is not None
            status = record.snapshot.status
            if status in TERMINAL_STATUSES:
                return self._public_snapshot(record)
            if status in {"pending", "queued"}:
                self._cleanup_partial_artifacts(record.snapshot)
                self._update_snapshot(
                    record,
                    status="cancelled",
                    progress_percent=record.snapshot.progress_percent,
                    finished_at=_utcnow(),
                    artifacts=[],
                )
                with contextlib.suppress(ValueError):
                    self._queue.remove(job_id)
            else:
                self._update_snapshot(
                    record,
                    status="cancel_requested",
                )
            record.cancel_event.set()
            return self._public_snapshot(record)

    def drain(self) -> DrainResponse:
        with self._state_lock:
            self._draining = True
            return self._drain_response()

    def resume(self) -> DrainResponse:
        with self._state_lock:
            if self._fatal or self._shutdown:
                raise WorkerUnavailable("fatal worker state requires a process restart")
            self._draining = False
            self._wake.set()
            return self._drain_response()

    def health_state(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "accepting_jobs": self._accepting_jobs(),
                "draining": self._draining,
                "recovering": self._recovering,
                "active_jobs": self._active_job_count(),
                "queued_jobs": len(self._queue),
                "worker_instance_id": self.worker_instance_id,
                "fatal": self._fatal,
                "fatal_reason": self._fatal_reason,
            }

    def artifact(self, job_id: str, artifact_id: str) -> ArtifactAccess:
        with self._state_lock:
            record = self._record(job_id)
        with record.artifact_io_lock:
            with self._state_lock:
                if self._records.get(job_id) is not record:
                    raise JobNotFound("unknown job_id")
                if job_id in self._purging_job_ids:
                    raise JobConflict("job deletion is in progress")
                if record.artifact_state != "available":
                    raise ArtifactNotFound("job artifacts are not available")
                descriptor = next(
                    (
                        item
                        for item in record.artifact_manifest
                        if item.artifact_id == artifact_id
                    ),
                    None,
                )
                snapshot = record.snapshot.model_copy(deep=True)
            if descriptor is None:
                raise ArtifactNotFound("artifact_id is not present in the job manifest")
            try:
                validate_artifact_name(descriptor.name)
            except ValueError as exc:
                raise RuntimeError("unsafe artifact name in manifest") from exc
            path = self._existing_artifact_directory(snapshot) / descriptor.name
            try:
                stream = open_verified_artifact(path, descriptor)
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                    raise ArtifactNotFound("manifest artifact is missing") from exc
                raise
            return ArtifactAccess(descriptor=descriptor, path=path, stream=stream)

    def bundle(self, job_id: str) -> BundleAccess:
        with self._state_lock:
            record = self._record(job_id)
        with record.artifact_io_lock:
            with self._state_lock:
                if self._records.get(job_id) is not record:
                    raise JobNotFound("unknown job_id")
                if job_id in self._purging_job_ids:
                    raise JobConflict("job deletion is in progress")
                if record.snapshot.status not in TERMINAL_STATUSES:
                    raise JobConflict(
                        "artifact bundle is available only for terminal jobs"
                    )
                if record.artifact_state != "available" or not record.artifact_manifest:
                    raise ArtifactNotFound("job has no artifacts")
                snapshot = record.snapshot.model_copy(deep=True)
                descriptors = tuple(record.artifact_manifest)
            artifact_directory = self._existing_artifact_directory(snapshot)
            manifest: list[tuple[ArtifactDescriptor, BinaryIO]] = []
            try:
                for descriptor in descriptors:
                    try:
                        validate_artifact_name(descriptor.name)
                    except ValueError as exc:
                        raise RuntimeError("unsafe artifact name in manifest") from exc
                    artifact_path = artifact_directory / descriptor.name
                    try:
                        artifact_stream = open_verified_artifact(
                            artifact_path, descriptor
                        )
                    except OSError as exc:
                        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
                            raise ArtifactNotFound(
                                "manifest artifact is missing"
                            ) from exc
                        raise
                    manifest.append((descriptor, artifact_stream))
                path = (
                    self._existing_attempt_directory(snapshot) / "artifact_bundle.zip"
                )
                bundle_stream = build_bundle(path, manifest)
            finally:
                for _, artifact_stream in manifest:
                    artifact_stream.close()

            try:
                metadata = os.fstat(bundle_stream.fileno())
                digest = sha256_open_file(bundle_stream)
            except BaseException:
                bundle_stream.close()
                raise
            return BundleAccess(
                path=path,
                stream=bundle_stream,
                size_bytes=metadata.st_size,
                sha256=digest,
            )

    def delete_artifacts(self, job_id: str) -> ArtifactDeletionResponse:
        with self._state_lock:
            record = self._record(job_id)
        with record.artifact_io_lock:
            with self._state_lock:
                if self._records.get(job_id) is not record:
                    raise JobNotFound("unknown job_id")
                if job_id in self._purging_job_ids:
                    raise JobConflict("job deletion is in progress")
                if record.snapshot.status not in TERMINAL_STATUSES:
                    raise JobConflict("artifacts can be deleted only for terminal jobs")
                if record.artifact_state in {"none", "deleted"}:
                    return ArtifactDeletionResponse(
                        job_id=job_id,
                        deleted=True,
                        artifact_state=(
                            "deleted" if record.artifact_state == "deleted" else "none"
                        ),
                        deleted_artifacts=0,
                        message="job artifacts are already absent",
                    )
                deleted = len(record.artifact_manifest)
                if record.artifact_state == "available":
                    self._update_snapshot(
                        record,
                        artifacts=[],
                        artifact_state="deleting",
                        artifact_delete_requested_at=_utcnow(),
                    )
            try:
                self._delete_artifact_files(record)
            except (JournalPersistenceError, KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                raise ArtifactDeletionFailed(
                    "artifact deletion is incomplete and will be resumed safely"
                ) from exc
            with self._state_lock:
                self._update_snapshot(
                    record,
                    artifacts=[],
                    artifact_state="deleted",
                    artifacts_deleted_at=_utcnow(),
                )
            return ArtifactDeletionResponse(
                job_id=job_id,
                deleted=True,
                artifact_state="deleted",
                deleted_artifacts=deleted,
                message="job artifacts were deleted; the durable journal was retained",
            )

    def purge_job(
        self,
        job_id: str,
        request: JobPurgeRequest,
    ) -> JobPurgeResponse:
        if _SAFE_JOB_ID.fullmatch(job_id) is None:
            raise JobNotFound("unknown job_id")
        job_directory = self.job_root / job_id
        tombstone_directory = self.job_root / f"{_PURGE_TOMBSTONE_PREFIX}{job_id}"

        with self._state_lock:
            record = self._records.get(job_id)
            if record is not None:
                self._validate_purge_identity(record, request)
                if record.snapshot.status not in TERMINAL_STATUSES:
                    raise JobConflict("only terminal jobs can be deleted")
                self._purging_job_ids.add(job_id)

        if record is None:
            deleted = self._resume_or_confirm_purge(
                job_id=job_id,
                job_directory=job_directory,
                tombstone_directory=tombstone_directory,
            )
            return JobPurgeResponse(
                job_id=job_id,
                storage_state="absent",
                deleted=deleted,
                message=(
                    "job storage deleted"
                    if deleted
                    else "job storage was already absent"
                ),
            )

        try:
            with record.artifact_io_lock:
                with self._state_lock:
                    if self._records.get(job_id) is not record:
                        raise JobConflict("job identity changed during deletion")
                    self._validate_purge_identity(record, request)
                    if record.snapshot.status not in TERMINAL_STATUSES:
                        raise JobConflict("only terminal jobs can be deleted")
                self._detach_job_directory(
                    job_directory=job_directory,
                    tombstone_directory=tombstone_directory,
                )
                with self._state_lock:
                    self._records.pop(job_id, None)
                    if self._sequence_to_job.get(record.enqueue_sequence) == job_id:
                        self._sequence_to_job.pop(record.enqueue_sequence, None)
                    with contextlib.suppress(ValueError):
                        self._queue.remove(job_id)
                self._remove_purge_tombstone(tombstone_directory)
        except BaseException:
            # A canonical directory means the detach did not commit and normal
            # operations may safely resume.  Once detached, the durable hidden
            # tombstone and missing in-memory record remain the deletion fence.
            if os.path.lexists(job_directory):
                with self._state_lock:
                    self._purging_job_ids.discard(job_id)
            raise
        with self._state_lock:
            self._purging_job_ids.discard(job_id)
        return JobPurgeResponse(
            job_id=job_id,
            storage_state="absent",
            deleted=True,
            message="job storage deleted",
        )

    @staticmethod
    def _validate_purge_identity(
        record: _JobRecord,
        request: JobPurgeRequest,
    ) -> None:
        if (
            record.snapshot.attempt_token != request.attempt_token
            or record.snapshot.request_sha256 != request.request_sha256
            or record.enqueue_sequence != request.enqueue_sequence
        ):
            raise JobConflict(
                "job deletion identity differs from the durable journal"
            )

    def _resume_or_confirm_purge(
        self,
        *,
        job_id: str,
        job_directory: Path,
        tombstone_directory: Path,
    ) -> bool:
        if os.path.lexists(job_directory):
            raise JobConflict(
                "job storage exists without a loaded durable identity"
            )
        deleted = os.path.lexists(tombstone_directory)
        if deleted:
            self._remove_purge_tombstone(tombstone_directory)
        else:
            self._fsync_directory(self.job_root)
        with self._state_lock:
            self._purging_job_ids.discard(job_id)
        return deleted

    async def _dispatch_loop(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                while True:
                    with self._state_lock:
                        if self._running_job_id is not None:
                            break
                        if self._shutdown or self._fatal:
                            return
                        if self._draining:
                            break
                        if not self._queue:
                            break
                        job_id = self._queue[0]
                        record = self._records[job_id]
                        now = _utcnow()
                        if record.gpu_wait_started_at is None:
                            record.gpu_wait_started_at = now
                            record.dispatch_queue_wait_ms = max(
                                0.0,
                                (now - record.snapshot.created_at).total_seconds()
                                * 1000.0,
                            )
                        self._running_job_id = job_id
                    await self._run(record, record.dispatch_queue_wait_ms)
                    with self._state_lock:
                        self._running_job_id = None
                        if self._shutdown or self._fatal:
                            return
                        if record.snapshot.status == "queued":
                            asyncio.get_running_loop().call_later(
                                0.25, self._wake.set
                            )
                            break
        except JournalPersistenceError:
            # _persist_snapshot already switched health to fatal/degraded.  End
            # the dispatcher normally so task exceptions cannot go unnoticed.
            with self._state_lock:
                self._running_job_id = None
            return
        except Exception:
            with self._state_lock:
                self._running_job_id = None
                self._mark_fatal("dispatcher_failure")
            return

    async def _run(self, record: _JobRecord, queue_wait_ms: float) -> None:
        snapshot = record.snapshot
        output_directory = self._artifact_directory(snapshot)
        ensure_private_directory(output_directory)
        event_loop = asyncio.get_running_loop()
        admission_event = asyncio.Event()
        timeout = (
            self.single_point_timeout_seconds
            if snapshot.request.calculation_type == "single_point"
            else self.optimization_timeout_seconds
        )

        def admission_cancelled() -> bool:
            with self._state_lock:
                return record.cancel_event.is_set() or (
                    record.snapshot.status == "queued"
                    and (self._draining or self._shutdown or self._fatal)
                )

        def admitted() -> float:
            with self._state_lock:
                if record.cancel_event.is_set():
                    raise ComputationCancelled("calculation was cancelled")
                if record.snapshot.status != "queued":
                    raise RuntimeError("GPU admission callback was invoked more than once")
                if self._draining or self._shutdown or self._fatal:
                    raise ComputationCancelled("GPU admission was paused by drain")
                if not self._queue or self._queue[0] != record.snapshot.job_id:
                    raise RuntimeError("FIFO head changed before GPU admission completed")
                now = _utcnow()
                record.admission_gpu_wait_ms = max(
                    0.0,
                    (now - (record.gpu_wait_started_at or now)).total_seconds()
                    * 1000.0,
                )
                timings = default_job_timings()
                timings["queue_wait_ms"] = queue_wait_ms
                timings["gpu_wait_ms"] = record.admission_gpu_wait_ms
                self._update_snapshot(
                    record,
                    status="running",
                    stage="validating",
                    progress_percent=1,
                    started_at=now,
                    timings=timings,
                )
                self._queue.popleft()
                event_loop.call_soon_threadsafe(admission_event.set)
                return record.admission_gpu_wait_ms

        def progress(stage: str, percent: int, message: str | None) -> None:
            del message
            with self._state_lock:
                if record.snapshot.status == "running":
                    self._update_snapshot(
                        record,
                        stage=stage,
                        progress_percent=max(
                            record.snapshot.progress_percent, min(99, int(percent))
                        ),
                    )
                elif record.snapshot.status == "cancel_requested":
                    return
                else:
                    raise RuntimeError(
                        "scientific progress arrived before GPU lease admission"
                    )

        execute_task = asyncio.create_task(
            asyncio.to_thread(
                self.engine.execute,
                snapshot.request,
                output_directory,
                admitted=admitted,
                progress=progress,
                cancelled=admission_cancelled,
                provenance=self._provenance(snapshot.request.model),
                queue_wait_ms=queue_wait_ms,
            )
        )
        # GPU capacity waiting is queue time, not scientific execution time.
        # Start the 600/1800 second calculation deadline only after the fenced
        # execution lease wins admission and queued -> running is durable.
        admission_task = asyncio.create_task(admission_event.wait())
        done, _ = await asyncio.wait(
            {execute_task, admission_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if execute_task in done:
            timed_out = False
        else:
            done, _ = await asyncio.wait({execute_task}, timeout=timeout)
            timed_out = not done
        if not admission_task.done():
            admission_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await admission_task
        if timed_out:
            record.cancel_event.set()
            terminator = getattr(self.runtime, "terminate_active", None)
            termination_proven = False
            if callable(terminator):
                try:
                    termination_proven = bool(
                        await asyncio.to_thread(terminator, "timeout")
                    )
                except BaseException:
                    # No signal is sent when Broker/MPS safety cannot be
                    # established. Keep the Worker drained/fatal while the
                    # execution thread remains accounted for.
                    with self._state_lock:
                        self._mark_fatal("gpu_termination_unproven")
            if not termination_proven:
                with self._state_lock:
                    self._mark_fatal("gpu_termination_unproven")

        execution: EngineExecution | None = None
        failure: BaseException | None = None
        try:
            # Never release the only GPU slot while the underlying thread is alive.
            execution = await execute_task
        except BaseException as exc:  # noqa: BLE001 - converted to a stable public error.
            failure = exc

        # A bounded Broker cancellation may return before its unique detached
        # ownership thread can prove grant-or-cancel. Preserve the durable FIFO
        # head, but stop dispatch and new admission until a process restart
        # gives the Broker a fresh owner identity. This also prevents a queued
        # head from retrying every 250 ms while its previous request ID is still
        # being recovered fail-closed.
        if self._runtime_admission_uncertain():
            self._mark_admission_uncertain()

        if timed_out or failure is not None or record.cancel_event.is_set():
            # Partial result trees can approach the artifact contract limits.
            # Keep rmtree/unlink off the UDS event loop on every terminal or
            # retryable failure path.
            await asyncio.to_thread(self._cleanup_partial_artifacts, record.snapshot)

        terminal_journal_error: JournalPersistenceError | None = None
        with self._state_lock:
            if timed_out:
                with contextlib.suppress(ValueError):
                    self._queue.remove(record.snapshot.job_id)
                self._finish_failed(
                    record,
                    StructuredError(
                        code="calculation_timeout",
                        message=(
                            f"Calculation exceeded the {int(timeout)} second "
                            "worker limit."
                        ),
                        retryable=True,
                    ),
                )
                return
            if isinstance(failure, JournalPersistenceError):
                with contextlib.suppress(ValueError):
                    self._queue.remove(record.snapshot.job_id)
                self._finish_failed(record, self._structured_failure(failure))
                return
            if (
                isinstance(failure, ComputationCancelled)
                and record.snapshot.status == "queued"
                and not record.cancel_event.is_set()
                and (self._draining or self._shutdown or self._fatal)
            ):
                return
            if record.cancel_event.is_set() or isinstance(failure, ComputationCancelled):
                self._update_snapshot(
                    record,
                    status="cancelled",
                    progress_percent=record.snapshot.progress_percent,
                    finished_at=_utcnow(),
                    error=None,
                    artifacts=[],
                )
                return
            if failure is not None:
                error = self._structured_failure(failure)
                if (
                    record.snapshot.status == "queued"
                    and error.retryable
                    and error.code
                    in {
                        "gpu_capacity_unavailable",
                        "gpu_runtime_unhealthy",
                        "gpu_lease_lost",
                    }
                ):
                    return
                with contextlib.suppress(ValueError):
                    self._queue.remove(record.snapshot.job_id)
                self._finish_failed(record, error)
                if error.code == "gpu_oom":
                    with contextlib.suppress(Exception):
                        self.runtime.empty_cuda_cache()
                elif error.code == "cuda_fatal":
                    self._mark_fatal("cuda_fatal")
                return
            assert execution is not None
            completed_timings = default_job_timings()
            completed_timings.update(
                {
                    key: float(value)
                    for key, value in execution.timings.items()
                    if key in completed_timings
                }
            )
            # The supervisor freezes GPU admission wait exactly when the
            # execution lease is granted. Child timings may observe individual
            # Broker calls, but must never be added a second time here.
            completed_timings["gpu_wait_ms"] = record.admission_gpu_wait_ms
            completed_result = dict(execution.result)
            completed_result["timings"] = dict(completed_timings)
            completed_manifest = tuple(
                descriptor for descriptor, _ in execution.artifacts
            )
            try:
                self._update_snapshot(
                    record,
                    status="completed",
                    stage="artifacts",
                    progress_percent=100,
                    finished_at=_utcnow(),
                    result=completed_result,
                    timings=completed_timings,
                    artifacts=list(completed_manifest),
                    artifact_state="available",
                    artifact_manifest=completed_manifest,
                    error=None,
                )
            except JournalPersistenceError as exc:
                # Completed files are not published unless their terminal
                # manifest is durable.  Leave recovery a running journal only.
                terminal_journal_error = exc

        if terminal_journal_error is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self._cleanup_partial_artifacts,
                    record.snapshot,
                )
            raise terminal_journal_error

    def _finish_failed(
        self,
        record: _JobRecord,
        error: StructuredError,
        *,
        stage: str | None = None,
    ) -> None:
        changes: dict[str, Any] = {
            "status": "failed",
            "finished_at": _utcnow(),
            "error": error,
            "artifacts": [],
        }
        if stage is not None:
            changes["stage"] = stage
        self._update_snapshot(record, **changes)

    @staticmethod
    def _structured_failure(exc: BaseException) -> StructuredError:
        if isinstance(exc, JournalPersistenceError):
            return StructuredError(
                code=exc.code,
                message="The worker could not durably persist job progress.",
                retryable=True,
            )
        if isinstance(exc, ChemistryValidationError):
            return StructuredError(
                code=exc.code,
                message=str(exc),
                retryable=False,
                details=exc.details,
            )
        if isinstance(exc, ScientificComputationError):
            return StructuredError(
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
                details=exc.details,
            )
        lowered = str(exc).lower()
        if isinstance(exc, MemoryError) or "out of memory" in lowered:
            return StructuredError(
                code="gpu_oom",
                message="The calculation exceeded available GPU memory.",
                retryable=True,
            )
        if any(
            marker in lowered
            for marker in (
                "cuda",
                "cublas",
                "cudnn",
                "device-side assert",
                "illegal memory access",
            )
        ):
            return StructuredError(
                code="cuda_fatal",
                message="The CUDA context entered an unsafe state and the worker must restart.",
                retryable=True,
            )
        if isinstance(exc, FileNotFoundError):
            return StructuredError(
                code="model_unavailable",
                message="A required local model asset is unavailable.",
                retryable=False,
            )
        return StructuredError(
            code="internal_error",
            message="The worker encountered an unexpected calculation error.",
            retryable=True,
            details={"exception_type": type(exc).__name__},
        )

    def _provenance(self, model: str) -> dict[str, Any]:
        probe = self.runtime.probe()
        payload = probe.to_dict() if hasattr(probe, "to_dict") else dict(probe)
        model_details = payload.get("models", {}).get(model, {})
        domain = MODEL_DOMAINS.get(model, {})
        return {
            "worker_version": self.worker_version,
            "worker_instance_id": self.worker_instance_id,
            "model_alias": model,
            "model_id": model,
            "model_registry_key": model_details.get("registry_key"),
            "model_family": model_details.get("family"),
            "model_reference": domain.get("implicit_solvation")
            or domain.get("family")
            or model_details.get("family"),
            "model_sha256": model_details.get("sha256"),
            "aimnet_version": payload.get("aimnet_version"),
            "aimnet_commit": payload.get("aimnet_commit"),
            "aimnet_wheel_sha256": payload.get("aimnet_wheel_sha256"),
            "warp_version": payload.get("warp_version"),
            "torch_version": payload.get("torch_version"),
            "cuda_runtime": payload.get("cuda_runtime"),
            "cuda_version": payload.get("cuda_runtime"),
            "gpu_name": payload.get("gpu_name"),
            "visible_gpu_count": payload.get("visible_gpu_count", 0),
            "logical_device": payload.get("logical_device", "cuda:0"),
            "physical_gpu": getattr(
                getattr(self.runtime, "settings", None), "physical_gpu", "3"
            ),
            "gpu_logical_device": payload.get("logical_device", "cuda:0"),
            "gpu_physical_device": getattr(
                getattr(self.runtime, "settings", None), "physical_gpu", "3"
            ),
        }

    def _accepting_jobs(self) -> bool:
        try:
            runtime_ready = bool(self.runtime.probe().ready)
        except Exception:
            runtime_ready = False
        return (
            runtime_ready
            and not self._draining
            and not self._recovering
            and not self._fatal
            and not self._shutdown
            and len(self._queue) < self.max_queued_jobs
        )

    def _drain_response(self) -> DrainResponse:
        return DrainResponse(
            status="draining" if self._draining else "ready",
            accepting_jobs=self._accepting_jobs(),
            active_jobs=self._active_job_count(),
            queued_jobs=len(self._queue),
            worker_instance_id=self.worker_instance_id,
        )

    def _record(self, job_id: str) -> _JobRecord:
        if job_id in self._purging_job_ids:
            raise JobConflict("job deletion is in progress")
        record = self._records.get(job_id)
        if record is None:
            raise JobNotFound("unknown job_id")
        return record

    def _record_or_none(self, job_id: str | None) -> _JobRecord | None:
        return self._records.get(job_id) if job_id is not None else None

    def _active_job_count(self) -> int:
        record = self._record_or_none(self._running_job_id)
        return int(
            record is not None
            and record.snapshot.status in {"running", "cancel_requested"}
        )

    def _public_snapshot(self, record: _JobRecord) -> PublicJobSnapshot:
        snapshot = record.snapshot.model_copy(deep=True)
        if record.artifact_state != "available":
            snapshot.artifacts = []
        if snapshot.status == "queued":
            try:
                snapshot.queue_position = list(self._queue).index(snapshot.job_id) + 1
            except ValueError:
                snapshot.queue_position = None
        else:
            snapshot.queue_position = None
        return PublicJobSnapshot.model_validate(
            {
                **snapshot.model_dump(mode="python"),
                "artifact_state": record.artifact_state,
            }
        )

    def _update_snapshot(self, record: _JobRecord, **changes: Any) -> None:
        artifact_state = changes.pop("artifact_state", record.artifact_state)
        artifact_manifest = changes.pop("artifact_manifest", record.artifact_manifest)
        artifact_delete_requested_at = changes.pop(
            "artifact_delete_requested_at", record.artifact_delete_requested_at
        )
        artifacts_deleted_at = changes.pop(
            "artifacts_deleted_at", record.artifacts_deleted_at
        )
        candidate = record.snapshot.model_copy(deep=True)
        for key, value in changes.items():
            setattr(candidate, key, value)
        candidate.queue_position = None
        candidate.updated_at = _utcnow()
        candidate = JobSnapshot.model_validate(candidate.model_dump(mode="python"))
        normalized_manifest = tuple(artifact_manifest)
        self._persist_record(
            record,
            snapshot=candidate,
            artifact_state=artifact_state,
            artifact_manifest=normalized_manifest,
            artifact_delete_requested_at=artifact_delete_requested_at,
            artifacts_deleted_at=artifacts_deleted_at,
        )
        record.snapshot = candidate
        record.artifact_state = artifact_state
        record.artifact_manifest = normalized_manifest
        record.artifact_delete_requested_at = artifact_delete_requested_at
        record.artifacts_deleted_at = artifacts_deleted_at

    def _persist_record(
        self,
        record: _JobRecord,
        *,
        snapshot: JobSnapshot | None = None,
        artifact_state: ArtifactState | None = None,
        artifact_manifest: tuple[ArtifactDescriptor, ...] | None = None,
        artifact_delete_requested_at: datetime | None | object = _UNSET,
        artifacts_deleted_at: datetime | None | object = _UNSET,
    ) -> None:
        active_snapshot = snapshot or record.snapshot
        active_state = artifact_state or record.artifact_state
        active_manifest = (
            artifact_manifest
            if artifact_manifest is not None
            else record.artifact_manifest
        )
        delete_requested = (
            record.artifact_delete_requested_at
            if artifact_delete_requested_at is _UNSET
            else artifact_delete_requested_at
        )
        deleted_at = (
            record.artifacts_deleted_at
            if artifacts_deleted_at is _UNSET
            else artifacts_deleted_at
        )
        envelope = JobJournalV2(
            snapshot=active_snapshot,
            enqueue_sequence=record.enqueue_sequence,
            enqueue_sequence_source=record.enqueue_sequence_source,
            artifact_state=active_state,
            artifact_manifest=list(active_manifest),
            artifact_delete_requested_at=delete_requested,
            artifacts_deleted_at=deleted_at,
        )
        try:
            self._write_record(envelope)
        except Exception as exc:
            self._mark_fatal("journal_persistence_failed")
            raise JournalPersistenceError(
                "job journal could not be persisted; worker restart is required"
            ) from exc

    def _mark_fatal(self, reason: str) -> None:
        schedule_exit = False
        with self._state_lock:
            if not self._fatal:
                self._fatal_reason = reason
            self._fatal = True
            self._draining = True
            running = self._record_or_none(self._running_job_id)
            if running is not None:
                running.cancel_event.set()

            loop = self._event_loop
            if loop is not None and loop.is_running():
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._wake.set)
            if (
                reason == "cuda_fatal"
                and self._fatal_exit is not None
                and not self._fatal_exit_scheduled
            ):
                restart_safe = getattr(self.runtime, "fatal_restart_safe", None)
                try:
                    schedule_exit = bool(
                        callable(restart_safe) and restart_safe()
                    )
                except Exception:
                    schedule_exit = False
                if schedule_exit:
                    self._fatal_exit_scheduled = True

        if schedule_exit:
            loop = self._event_loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._fatal_exit)

    def _runtime_admission_uncertain(self) -> bool:
        try:
            value = getattr(self.runtime, "admission_uncertain", False)
            return bool(value() if callable(value) else value)
        except Exception:
            return True

    def _mark_admission_uncertain(self) -> None:
        with self._state_lock:
            if not self._fatal:
                self._fatal_reason = "gpu_admission_uncertain"
            self._fatal = True
            self._draining = True
            loop = self._event_loop
            if loop is not None and loop.is_running():
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._wake.set)

    def _recover_purge_tombstones(self) -> None:
        changed = False
        for child in sorted(self.job_root.iterdir(), key=lambda path: path.name):
            if not child.name.startswith(_PURGE_TOMBSTONE_PREFIX):
                continue
            job_id = child.name[len(_PURGE_TOMBSTONE_PREFIX) :]
            if _SAFE_JOB_ID.fullmatch(job_id) is None:
                raise RuntimeError("unsafe DFT purge tombstone name")
            canonical = self.job_root / job_id
            if os.path.lexists(canonical):
                raise RuntimeError(
                    "DFT job directory and purge tombstone coexist"
                )
            if child.is_symlink() or not child.is_dir():
                raise RuntimeError("unsafe DFT purge tombstone")
            shutil.rmtree(child)
            changed = True
        if changed:
            self._fsync_directory(self.job_root)

    def _detach_job_directory(
        self,
        *,
        job_directory: Path,
        tombstone_directory: Path,
    ) -> None:
        for path in (job_directory, tombstone_directory):
            if path.is_symlink():
                raise RuntimeError("unsafe DFT job deletion path")
        job_exists = os.path.lexists(job_directory)
        tombstone_exists = os.path.lexists(tombstone_directory)
        if job_exists and tombstone_exists:
            raise RuntimeError(
                "DFT job directory and purge tombstone coexist"
            )
        if tombstone_exists:
            if not tombstone_directory.is_dir():
                raise RuntimeError("unsafe DFT purge tombstone")
            return
        if not job_exists or not job_directory.is_dir():
            raise RuntimeError(
                "loaded DFT job lacks its durable job directory"
            )
        os.rename(job_directory, tombstone_directory)
        self._fsync_directory(self.job_root)

    def _remove_purge_tombstone(self, tombstone_directory: Path) -> None:
        if not os.path.lexists(tombstone_directory):
            self._fsync_directory(self.job_root)
            return
        if tombstone_directory.is_symlink() or not tombstone_directory.is_dir():
            raise RuntimeError("unsafe DFT purge tombstone")
        shutil.rmtree(tombstone_directory)
        self._fsync_directory(self.job_root)

    def _job_directory(self, job_id: str) -> Path:
        path = self.job_root / job_id
        ensure_private_directory(path)
        return path

    def _attempt_directory(self, snapshot: JobSnapshot) -> Path:
        path = self._job_directory(snapshot.job_id) / snapshot.attempt_token
        ensure_private_directory(path)
        return path

    def _artifact_directory(self, snapshot: JobSnapshot) -> Path:
        path = self._attempt_directory(snapshot) / "artifacts"
        ensure_private_directory(path)
        return path

    def _existing_attempt_directory(self, snapshot: JobSnapshot) -> Path:
        job_directory = self.job_root / snapshot.job_id
        attempt_directory = job_directory / snapshot.attempt_token
        for path in (job_directory, attempt_directory):
            if path.is_symlink() or not path.is_dir():
                raise ArtifactNotFound("job attempt directory is missing")
        return attempt_directory

    def _existing_artifact_directory(self, snapshot: JobSnapshot) -> Path:
        path = self._existing_attempt_directory(snapshot) / "artifacts"
        if path.is_symlink() or not path.is_dir():
            raise ArtifactNotFound("job artifact directory is missing")
        return path

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _delete_artifact_files(self, record: _JobRecord) -> None:
        attempt_directory = self._existing_attempt_directory(record.snapshot)
        artifact_directory = attempt_directory / "artifacts"
        tombstone_directory = attempt_directory / ".artifacts.deleting"
        bundle_path = attempt_directory / "artifact_bundle.zip"
        bundle_creating = attempt_directory / BUNDLE_CREATING_NAME
        bundle_tombstone = attempt_directory / ".artifact_bundle.zip.deleting"
        for path in (
            artifact_directory,
            tombstone_directory,
            bundle_path,
            bundle_creating,
            bundle_tombstone,
        ):
            if path.is_symlink():
                raise RuntimeError("unsafe artifact deletion path")
        if artifact_directory.exists() and tombstone_directory.exists():
            raise RuntimeError("artifact directory and deletion tombstone coexist")
        if bundle_path.exists() and bundle_tombstone.exists():
            raise RuntimeError("artifact bundle and deletion tombstone coexist")

        if bundle_creating.exists():
            if not bundle_creating.is_file():
                raise RuntimeError("artifact bundle creation path is not a regular file")
            bundle_creating.unlink()
            self._fsync_directory(attempt_directory)

        renamed = False
        if artifact_directory.exists():
            if not artifact_directory.is_dir():
                raise RuntimeError("artifact path is not a directory")
            os.rename(artifact_directory, tombstone_directory)
            renamed = True
        if bundle_path.exists():
            if not bundle_path.is_file():
                raise RuntimeError("artifact bundle is not a regular file")
            os.rename(bundle_path, bundle_tombstone)
            renamed = True
        if renamed:
            self._fsync_directory(attempt_directory)

        if tombstone_directory.exists():
            if tombstone_directory.is_symlink() or not tombstone_directory.is_dir():
                raise RuntimeError("unsafe artifact deletion tombstone")
            shutil.rmtree(tombstone_directory)
        if bundle_tombstone.exists():
            if bundle_tombstone.is_symlink() or not bundle_tombstone.is_file():
                raise RuntimeError("unsafe artifact bundle deletion tombstone")
            bundle_tombstone.unlink()
        self._fsync_directory(attempt_directory)

    def _cleanup_partial_artifacts(self, snapshot: JobSnapshot) -> None:
        job_directory = self.job_root / snapshot.job_id
        attempt_directory = job_directory / snapshot.attempt_token
        for path in (job_directory, attempt_directory):
            if path.is_symlink():
                raise RuntimeError("unsafe job path while cleaning partial artifacts")
            if not path.exists():
                return
            if not path.is_dir():
                raise RuntimeError("job path is not a directory")
        artifact_directory = attempt_directory / "artifacts"
        if artifact_directory.is_symlink():
            raise RuntimeError("unsafe partial artifact directory")
        if artifact_directory.exists():
            if not artifact_directory.is_dir():
                raise RuntimeError("partial artifact path is not a directory")
            shutil.rmtree(artifact_directory)
        tombstone_directory = attempt_directory / ".artifacts.deleting"
        if tombstone_directory.is_symlink():
            raise RuntimeError("unsafe partial artifact tombstone")
        if tombstone_directory.exists():
            if not tombstone_directory.is_dir():
                raise RuntimeError("partial artifact tombstone is not a directory")
            shutil.rmtree(tombstone_directory)
        bundle = attempt_directory / "artifact_bundle.zip"
        if bundle.is_symlink():
            raise RuntimeError("unsafe partial artifact bundle")
        if bundle.exists():
            if not bundle.is_file():
                raise RuntimeError("partial artifact bundle is not a file")
            bundle.unlink()
        bundle_creating = attempt_directory / BUNDLE_CREATING_NAME
        if bundle_creating.is_symlink():
            raise RuntimeError("unsafe partial artifact bundle creation path")
        if bundle_creating.exists():
            if not bundle_creating.is_file():
                raise RuntimeError(
                    "partial artifact bundle creation path is not a file"
                )
            bundle_creating.unlink()
        bundle_tombstone = attempt_directory / ".artifact_bundle.zip.deleting"
        if bundle_tombstone.is_symlink():
            raise RuntimeError("unsafe partial artifact bundle tombstone")
        if bundle_tombstone.exists():
            if not bundle_tombstone.is_file():
                raise RuntimeError("partial artifact bundle tombstone is not a file")
            bundle_tombstone.unlink()

    def _recover_bundle_creation(self, snapshot: JobSnapshot) -> None:
        attempt_directory = self._existing_attempt_directory(snapshot)
        creating = attempt_directory / BUNDLE_CREATING_NAME
        if creating.is_symlink():
            raise RuntimeError("unsafe artifact bundle creation recovery path")
        if not creating.exists():
            return
        if not creating.is_file():
            raise RuntimeError("artifact bundle creation recovery path is not a file")
        creating.unlink()
        self._fsync_directory(attempt_directory)

    def _write_record(self, envelope: JobJournalV2) -> None:
        path = self._attempt_directory(envelope.snapshot) / "journal.json"
        self._journal_writer(path, envelope.model_dump(mode="json"))
