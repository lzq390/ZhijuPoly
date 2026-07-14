from __future__ import annotations

import inspect
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_for_futures
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from typing import Callable

from app.models import (
    ConditionalGenerationJobStatusResponse,
    ConditionalGenerationTgRequest,
    ConditionalGenerationTgResponse,
)
from app.services.gpu_runtime_registry import (
    GpuQueueFullError,
    GpuQueueTimeoutError,
    GpuSchedulerClosedError,
)
from app.services.in_memory_jobs import (
    BoundedInMemoryJobStore,
    JobStoreCapacityError,
)


NAMESPACE = "conditional_generation"
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class ConditionalGenerationJobCapacityError(RuntimeError):
    """Raised when all conditional-generation execution slots are occupied."""

    def __init__(self, *, active_jobs: int, max_active_jobs: int) -> None:
        self.active_jobs = active_jobs
        self.max_active_jobs = max_active_jobs
        super().__init__(
            "Conditional generation job capacity is full "
            f"({active_jobs}/{max_active_jobs} pending or running jobs)"
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class _ConditionalGenerationJob:
    job_id: str
    request: ConditionalGenerationTgRequest
    accepted_deadline: float
    status: str = "pending"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    attempts: int = 0
    accepted_count: int = 0
    message: str | None = None
    error: str | None = None
    result: ConditionalGenerationTgResponse | None = None


JobRunner = Callable[..., ConditionalGenerationTgResponse]


class ConditionalGenerationJobManager:
    def __init__(
        self,
        *,
        max_workers: int = 1,
        max_active_jobs: int = 8,
        store: BoundedInMemoryJobStore | None = None,
        monotonic_fn: Callable[[], float] = monotonic,
    ) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="conditional-generation",
        )
        self._max_active_jobs = max(1, max_active_jobs)
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
        request: ConditionalGenerationTgRequest,
        runner: JobRunner,
        *,
        timeout_seconds: float = 600.0,
    ) -> ConditionalGenerationJobStatusResponse:
        accepted_deadline = self._monotonic() + max(0.0, float(timeout_seconds))
        with self._lock:
            if self._shutdown_started:
                raise RuntimeError("Conditional generation job manager is shutting down")
            active_jobs = len(self._futures)
            if active_jobs >= self._max_active_jobs:
                raise ConditionalGenerationJobCapacityError(
                    active_jobs=active_jobs,
                    max_active_jobs=self._max_active_jobs,
                )
            job = self._store.create(
                NAMESPACE,
                lambda job_id: _ConditionalGenerationJob(
                    job_id=job_id,
                    request=request,
                    accepted_deadline=accepted_deadline,
                ),
            )
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

    def get_job(self, job_id: str) -> ConditionalGenerationJobStatusResponse:
        return self._store.read(NAMESPACE, job_id, self._snapshot)

    def cancel_job(self, job_id: str) -> ConditionalGenerationJobStatusResponse:
        # Validate the public identifier even when its Future has already left
        # the active map, then only cancel work that has not started.
        self.get_job(job_id)
        with self._lock:
            future = self._futures.get(job_id)
        if future is not None:
            future.cancel()
        return self.get_job(job_id)

    def shutdown(
        self,
        *,
        wait: bool = False,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Stop admission, cancel queued work, and optionally await live futures.

        A finite timeout lets the FastAPI lifespan stay inside the container's
        graceful-stop budget. Running work keeps its record active until the
        future actually completes, even when the timeout is exhausted.
        """
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

    def stop_accepting(self) -> None:
        with self._lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True

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
            response = _invoke_runner(runner, remaining)
        except GpuSchedulerClosedError as exc:
            self._mark_cancelled(job_id, str(exc))
            return
        except (GpuQueueFullError, GpuQueueTimeoutError) as exc:
            self._mark_failed(job_id, str(exc))
            return
        except Exception as exc:  # pragma: no cover - exercised through API/runtime failures
            self._mark_failed(job_id, str(exc))
            return

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

    def _mark_running(self, job_id: str) -> bool:
        def update(job: _ConditionalGenerationJob) -> bool:
            if job.status in TERMINAL_STATUSES:
                return False
            job.status = "running"
            job.started_at = _utc_now()
            job.updated_at = job.started_at
            return True

        return self._store.mutate(NAMESPACE, job_id, update)

    def _mark_failed(self, job_id: str, error: str) -> None:
        error = str(error)[:2000]

        def update(job: _ConditionalGenerationJob) -> None:
            if job.status in TERMINAL_STATUSES:
                return
            job.status = "failed"
            job.error = error
            job.message = None
            job.updated_at = _utc_now()
            job.finished_at = job.updated_at

        self._store.mutate(NAMESPACE, job_id, update, terminal=True)

    def _mark_cancelled(self, job_id: str, message: str) -> None:
        message = str(message)[:2000]

        def update(job: _ConditionalGenerationJob) -> None:
            if job.status in TERMINAL_STATUSES:
                return
            job.status = "cancelled"
            job.message = message
            job.updated_at = _utc_now()
            job.finished_at = job.updated_at

        self._store.mutate(NAMESPACE, job_id, update, terminal=True)

    def _on_future_done(self, job_id: str, future: Future[None]) -> None:
        try:
            if future.cancelled():
                self._mark_cancelled(
                    job_id,
                    "Conditional generation was cancelled before execution.",
                )
            else:
                unexpected_error = future.exception()
                if unexpected_error is not None:
                    self._mark_failed(
                        job_id,
                        "Conditional generation task terminated unexpectedly: "
                        f"{unexpected_error}",
                    )
        finally:
            # Keep the execution slot occupied until its terminal record can
            # participate in retention eviction. Otherwise a concurrent
            # submit can observe a free lane but a spuriously full store.
            self._store.mark_reapable(NAMESPACE, job_id)
            with self._lock:
                if self._futures.get(job_id) is future:
                    self._futures.pop(job_id, None)

    @staticmethod
    def _complete_job(
        job: _ConditionalGenerationJob,
        response: ConditionalGenerationTgResponse,
    ) -> None:
        if job.status in TERMINAL_STATUSES:
            return
        job.status = "completed"
        job.message = f"Generated {response.returned_count} candidates."
        job.error = None
        job.result = response
        job.attempts = response.attempts
        job.accepted_count = response.returned_count
        job.updated_at = _utc_now()
        job.finished_at = job.updated_at

    @staticmethod
    def _snapshot(job: _ConditionalGenerationJob) -> ConditionalGenerationJobStatusResponse:
        return ConditionalGenerationJobStatusResponse(
            job_id=job.job_id,
            status=job.status,  # type: ignore[arg-type]
            delta_tg=job.request.delta_tg,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            attempts=job.attempts,
            accepted_count=job.accepted_count,
            message=job.message,
            error=job.error,
            result=job.result,
        )


def _invoke_runner(runner: JobRunner, remaining_seconds: float) -> ConditionalGenerationTgResponse:
    """Pass the accepted-time budget while preserving focused zero-arg test runners."""
    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        return runner(remaining_seconds)
    try:
        signature.bind(remaining_seconds)
    except TypeError:
        return runner()
    return runner(remaining_seconds)
