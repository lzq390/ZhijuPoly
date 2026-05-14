from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event, Lock
from typing import Callable
from uuid import uuid4

from app.models import (
    ReverseDesignTgJobStatusResponse,
    ReverseDesignTgRequest,
    ReverseDesignTgResponse,
)


TerminalStatus = {"found_enough", "exhausted", "failed", "cancelled"}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class _ReverseDesignJob:
    job_id: str
    request: ReverseDesignTgRequest
    status: str = "pending"
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    scanned_rows: int = 0
    matched_count: int = 0
    current_tg_radius: float | None = None
    best_similarity_score: float | None = None
    message: str | None = None
    error: str | None = None
    result: ReverseDesignTgResponse | None = None
    cancel_event: Event = field(default_factory=Event)
    future: Future | None = None


ProgressCallback = Callable[..., None]
CancellationCheck = Callable[[], bool]
JobRunner = Callable[[ProgressCallback, CancellationCheck], ReverseDesignTgResponse]


class ReverseDesignJobManager:
    def __init__(self, *, max_workers: int = 1) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="reverse-design")
        self._jobs: dict[str, _ReverseDesignJob] = {}
        self._lock = Lock()

    def create_job(self, request: ReverseDesignTgRequest, runner: JobRunner) -> ReverseDesignTgJobStatusResponse:
        job = _ReverseDesignJob(job_id=uuid4().hex, request=request)
        with self._lock:
            self._jobs[job.job_id] = job
        job.future = self._executor.submit(self._run_job, job.job_id, runner)
        return self.get_job(job.job_id)

    def get_job(self, job_id: str) -> ReverseDesignTgJobStatusResponse:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return self._snapshot(job)

    def cancel_job(self, job_id: str) -> ReverseDesignTgJobStatusResponse:
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

    def wait_for_job(self, job_id: str, timeout: float | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            future = job.future
        if future is not None:
            future.result(timeout=timeout)

    def shutdown(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def _run_job(self, job_id: str, runner: JobRunner) -> None:
        self._mark_running(job_id)
        try:
            response = runner(
                lambda **progress: self._update_progress(job_id, **progress),
                lambda: self._is_cancelled(job_id),
            )
        except Exception as exc:  # pragma: no cover - exercised through API/runtime failures
            self._mark_failed(job_id, str(exc))
            return

        with self._lock:
            job = self._jobs[job_id]
            if job.cancel_event.is_set():
                job.status = "cancelled"
                job.message = "Reverse-design search was cancelled."
            elif response.total >= 200:
                job.status = "found_enough"
                job.message = "Found 200 candidates that satisfy the similarity threshold."
            else:
                job.status = "exhausted"
                job.message = "The PI database was fully scanned before 200 candidates were found."
            job.result = response
            job.matched_count = response.total
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

    def _update_progress(self, job_id: str, **progress: object) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.scanned_rows = int(progress.get("scanned_rows", job.scanned_rows) or 0)
            job.matched_count = int(progress.get("matched_count", job.matched_count) or 0)
            job.current_tg_radius = _optional_float(progress.get("current_tg_radius", job.current_tg_radius))
            job.best_similarity_score = _optional_float(progress.get("best_similarity_score", job.best_similarity_score))
            job.updated_at = _utc_now()

    def _is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs[job_id]
            return job.cancel_event.is_set()

    def _snapshot(self, job: _ReverseDesignJob) -> ReverseDesignTgJobStatusResponse:
        return ReverseDesignTgJobStatusResponse(
            job_id=job.job_id,
            status=job.status,  # type: ignore[arg-type]
            target_tg=job.request.target_tg,
            similarity_threshold=job.request.similarity_threshold,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            scanned_rows=job.scanned_rows,
            matched_count=job.matched_count,
            current_tg_radius=job.current_tg_radius,
            best_similarity_score=job.best_similarity_score,
            message=job.message,
            error=job.error,
            result=job.result,
        )


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
