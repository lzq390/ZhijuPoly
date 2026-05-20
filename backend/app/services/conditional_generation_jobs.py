from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event, Lock
from typing import Callable
from uuid import uuid4

from app.models import (
    ConditionalGenerationJobStatusResponse,
    ConditionalGenerationTgRequest,
    ConditionalGenerationTgResponse,
)


TerminalStatus = {"completed", "failed", "cancelled"}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class _ConditionalGenerationJob:
    job_id: str
    request: ConditionalGenerationTgRequest
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
    cancel_event: Event = field(default_factory=Event)
    future: Future | None = None


JobRunner = Callable[[], ConditionalGenerationTgResponse]


class ConditionalGenerationJobManager:
    def __init__(self, *, max_workers: int = 1) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="conditional-generation",
        )
        self._jobs: dict[str, _ConditionalGenerationJob] = {}
        self._lock = Lock()

    def create_job(
        self,
        request: ConditionalGenerationTgRequest,
        runner: JobRunner,
    ) -> ConditionalGenerationJobStatusResponse:
        job = _ConditionalGenerationJob(job_id=uuid4().hex, request=request)
        with self._lock:
            self._jobs[job.job_id] = job
        job.future = self._executor.submit(self._run_job, job.job_id, runner)
        return self.get_job(job.job_id)

    def get_job(self, job_id: str) -> ConditionalGenerationJobStatusResponse:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return self._snapshot(job)

    def cancel_job(self, job_id: str) -> ConditionalGenerationJobStatusResponse:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job.cancel_event.set()
            if job.status not in TerminalStatus:
                job.status = "cancelled"
                job.updated_at = _utc_now()
                job.finished_at = job.updated_at
        return self.get_job(job_id)

    def shutdown(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def _run_job(self, job_id: str, runner: JobRunner) -> None:
        self._mark_running(job_id)
        try:
            response = runner()
        except Exception as exc:  # pragma: no cover - exercised through API/runtime failures
            self._mark_failed(job_id, str(exc))
            return

        with self._lock:
            job = self._jobs[job_id]
            if job.cancel_event.is_set():
                job.status = "cancelled"
                job.message = "Conditional generation was cancelled."
            else:
                job.status = "completed"
                job.message = f"Generated {response.returned_count} candidates."
            job.result = response
            job.attempts = response.attempts
            job.accepted_count = response.returned_count
            job.updated_at = _utc_now()
            job.finished_at = job.updated_at

    def _mark_running(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
            job.started_at = _utc_now()
            job.updated_at = job.started_at

    def _mark_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "failed"
            job.error = error
            job.updated_at = _utc_now()
            job.finished_at = job.updated_at

    def _snapshot(self, job: _ConditionalGenerationJob) -> ConditionalGenerationJobStatusResponse:
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
