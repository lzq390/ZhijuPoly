from __future__ import annotations

import inspect
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_for_futures
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from typing import Callable

from app.models import PolytaoGenerationResponse, PolytaoJobStatusResponse
from app.services.gpu_runtime_registry import (
    GpuQueueFullError,
    GpuQueueTimeoutError,
    GpuSchedulerClosedError,
)
from app.services.in_memory_jobs import BoundedInMemoryJobStore, JobStoreCapacityError
from app.services.polytao_runtime import PolytaoGenerationResult


NAMESPACE = "polytao"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class PolytaoJobCapacityError(RuntimeError):
    def __init__(self, *, active_jobs: int, max_active_jobs: int) -> None:
        self.active_jobs = active_jobs
        self.max_active_jobs = max_active_jobs
        super().__init__(
            f"PolyTAO job capacity is full ({active_jobs}/{max_active_jobs} pending or running jobs)"
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class _PolytaoJob:
    job_id: str
    input_smiles: str | None
    canonical_smiles: str | None
    prompt: str
    requested_count: int
    accepted_deadline: float
    status: str = "pending"
    returned_count: int = 0
    attempts: int = 0
    progress_percent: int = 0
    progress_stage: str = "pending"
    progress_message: str = "Waiting for the PolyTAO backend runtime to start."
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None
    result: PolytaoGenerationResponse | None = None


JobRunner = Callable[..., PolytaoGenerationResult | PolytaoGenerationResponse]


class PolytaoJobManager:
    """Run and retain PolyTAO jobs entirely inside the backend process."""

    def __init__(
        self,
        *,
        max_workers: int = 1,
        max_active_jobs: int = 1,
        store: BoundedInMemoryJobStore | None = None,
        monotonic_fn: Callable[[], float] = monotonic,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="polytao-backend",
        )
        self._max_active_jobs = max(1, int(max_active_jobs))
        self._store = store or BoundedInMemoryJobStore()
        self._monotonic = monotonic_fn
        self._futures: dict[str, Future[None]] = {}
        self._lock = Lock()
        self._shutdown_started = False

    @property
    def active_jobs(self) -> int:
        with self._lock:
            return len(self._futures)

    @property
    def active_executions(self) -> int:
        return self.active_jobs

    @property
    def active_futures(self) -> int:
        return self.active_jobs

    @property
    def max_active_jobs(self) -> int:
        return self._max_active_jobs

    @property
    def accepting(self) -> bool:
        with self._lock:
            return not self._shutdown_started

    @property
    def retained_jobs(self) -> int:
        return self._store.stats(NAMESPACE).jobs

    @property
    def retained_bytes(self) -> int:
        return self._store.stats(NAMESPACE).bytes

    def create_job(
        self,
        *,
        input_smiles: str | None,
        canonical_smiles: str | None,
        prompt: str,
        requested_count: int,
        runner: JobRunner,
        timeout_seconds: float = 600.0,
    ) -> PolytaoJobStatusResponse:
        accepted_deadline = self._monotonic() + max(0.0, float(timeout_seconds))
        with self._lock:
            if self._shutdown_started:
                raise RuntimeError("PolyTAO job manager is shutting down")
            active_jobs = len(self._futures)
            if active_jobs >= self._max_active_jobs:
                raise PolytaoJobCapacityError(
                    active_jobs=active_jobs,
                    max_active_jobs=self._max_active_jobs,
                )
            job = self._store.create(
                NAMESPACE,
                lambda job_id: _PolytaoJob(
                    job_id=job_id,
                    input_smiles=input_smiles,
                    canonical_smiles=canonical_smiles,
                    prompt=prompt,
                    requested_count=requested_count,
                    accepted_deadline=accepted_deadline,
                ),
            )
            self._mark_submitted(job.job_id)
            try:
                future = self._executor.submit(self._run_job, job.job_id, runner)
            except Exception:
                self._store.delete(NAMESPACE, job.job_id)
                raise
            self._futures[job.job_id] = future
        snapshot = self.get_job(job.job_id)
        future.add_done_callback(
            lambda completed_future, submitted_job_id=job.job_id: self._on_future_done(
                submitted_job_id,
                completed_future,
            )
        )
        return snapshot

    def get_job(self, job_id: str) -> PolytaoJobStatusResponse:
        return self._store.read(NAMESPACE, job_id, self._snapshot)

    def cancel_job(self, job_id: str) -> PolytaoJobStatusResponse:
        self.get_job(job_id)
        with self._lock:
            future = self._futures.get(job_id)
        if future is not None:
            future.cancel()
        return self.get_job(job_id)

    def stop_accepting(self) -> None:
        with self._lock:
            self._shutdown_started = True

    def shutdown(
        self,
        *,
        wait: bool = False,
        timeout_seconds: float | None = None,
    ) -> bool:
        self.stop_accepting()
        futures = self.cancel_pending()
        completed = self.wait_for_futures(futures, timeout_seconds=timeout_seconds) if wait else not futures
        self.close_executor(wait=wait and completed)
        return completed

    def cancel_pending(self) -> tuple[Future[None], ...]:
        with self._lock:
            futures = tuple(self._futures.values())
        for future in futures:
            future.cancel()
        return futures

    @staticmethod
    def wait_for_futures(
        futures: tuple[Future[None], ...],
        *,
        timeout_seconds: float | None,
    ) -> bool:
        if not futures:
            return True
        _, unfinished = wait_for_futures(
            futures,
            timeout=None if timeout_seconds is None else max(0.0, float(timeout_seconds)),
        )
        return not unfinished

    def close_executor(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _run_job(self, job_id: str, runner: JobRunner) -> None:
        remaining = self._store.read(
            NAMESPACE,
            job_id,
            lambda job: job.accepted_deadline - self._monotonic(),
        )
        if remaining <= 0:
            self._mark_failed(
                job_id,
                "GPU_QUEUE_TIMEOUT: accepted job deadline expired before GPU execution",
            )
            return
        if not self._mark_running(job_id):
            return
        try:
            raw_result = _invoke_runner(runner, remaining)
        except GpuSchedulerClosedError as exc:
            self._mark_cancelled(job_id, str(exc))
            return
        except (GpuQueueFullError, GpuQueueTimeoutError) as exc:
            self._mark_failed(job_id, str(exc))
            return
        except Exception as exc:  # pragma: no cover - exercised through route/runtime tests
            self._mark_failed(job_id, str(exc))
            return

        response = (
            raw_result
            if isinstance(raw_result, PolytaoGenerationResponse)
            else PolytaoGenerationResponse.model_validate(raw_result.result)
        )
        try:
            self._store.mutate(
                NAMESPACE,
                job_id,
                lambda job: self._complete_job(job, response),
                terminal=True,
            )
        except JobStoreCapacityError:
            self._mark_failed(
                job_id,
                "JOB_RESULT_RETENTION_LIMIT: generated result exceeds in-memory retention capacity",
            )

    def _mark_submitted(self, job_id: str) -> None:
        def update(job: _PolytaoJob) -> None:
            if job.status != "pending":
                return
            job.status = "submitted"
            job.progress_stage = "submitted"
            job.progress_message = "Submitted to the PolyTAO backend runtime."
            job.updated_at = _utc_now()

        self._store.mutate(NAMESPACE, job_id, update)

    def _mark_running(self, job_id: str) -> bool:
        def update(job: _PolytaoJob) -> bool:
            if job.status in TERMINAL_STATUSES:
                return False
            job.status = "running"
            job.attempts = max(1, job.attempts)
            job.progress_percent = 10
            job.progress_stage = "running"
            job.progress_message = "Running PolyTAO generation in the backend runtime."
            job.started_at = job.started_at or _utc_now()
            job.updated_at = _utc_now()
            return True

        return self._store.mutate(NAMESPACE, job_id, update)

    def _mark_failed(self, job_id: str, error_message: str) -> None:
        error_message = str(error_message)[:2000]

        def update(job: _PolytaoJob) -> None:
            if job.status in TERMINAL_STATUSES:
                return
            job.status = "failed"
            job.progress_stage = "failed"
            job.progress_message = error_message
            job.error_message = error_message
            job.updated_at = _utc_now()
            job.finished_at = job.updated_at

        self._store.mutate(NAMESPACE, job_id, update, terminal=True)

    def _mark_cancelled(self, job_id: str, message: str) -> None:
        message = str(message)[:2000]

        def update(job: _PolytaoJob) -> None:
            if job.status in TERMINAL_STATUSES:
                return
            job.status = "cancelled"
            job.progress_stage = "cancelled"
            job.progress_message = message
            job.error_message = message
            job.updated_at = _utc_now()
            job.finished_at = job.updated_at

        self._store.mutate(NAMESPACE, job_id, update, terminal=True)

    def _on_future_done(self, job_id: str, future: Future[None]) -> None:
        try:
            if future.cancelled():
                self._mark_cancelled(job_id, "PolyTAO job was cancelled before execution.")
            else:
                unexpected_error = future.exception()
                if unexpected_error is not None:
                    self._mark_failed(
                        job_id,
                        f"PolyTAO backend task terminated unexpectedly: {unexpected_error}",
                    )
        finally:
            self._store.mark_reapable(NAMESPACE, job_id)
            with self._lock:
                if self._futures.get(job_id) is future:
                    self._futures.pop(job_id, None)

    @staticmethod
    def _complete_job(job: _PolytaoJob, response: PolytaoGenerationResponse) -> None:
        if job.status in TERMINAL_STATUSES:
            return
        job.status = "completed"
        job.returned_count = response.returned_count
        job.attempts = response.attempts
        job.progress_percent = 100
        job.progress_stage = "completed"
        job.progress_message = "PolyTAO generation completed in the backend runtime."
        job.error_message = None
        job.result = response
        job.updated_at = _utc_now()
        job.finished_at = job.updated_at

    @staticmethod
    def _snapshot(job: _PolytaoJob) -> PolytaoJobStatusResponse:
        return PolytaoJobStatusResponse(
            job_id=job.job_id,
            status=job.status,  # type: ignore[arg-type]
            input_smiles=job.input_smiles,
            canonical_smiles=job.canonical_smiles,
            prompt=job.prompt,
            requested_count=job.requested_count,
            returned_count=job.returned_count,
            attempts=job.attempts,
            progress_percent=job.progress_percent,
            progress_stage=job.progress_stage,
            progress_message=job.progress_message,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            worker_id=None,
            worker_job_id=None,
            worker_version=None,
            engine="polytao-backend",
            error_message=job.error_message,
            result=job.result,
        )


def _invoke_runner(
    runner: JobRunner,
    remaining_seconds: float,
) -> PolytaoGenerationResult | PolytaoGenerationResponse:
    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        return runner(remaining_seconds)
    try:
        signature.bind(remaining_seconds)
    except TypeError:
        return runner()
    return runner(remaining_seconds)
