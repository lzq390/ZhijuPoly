from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_for_futures
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


class ReverseDesignJobUnavailableError(RuntimeError):
    """Raised when a reverse-design job cannot be admitted for execution."""


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
        self._shutdown_started = False

    @property
    def active_jobs(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status not in TerminalStatus)

    @property
    def active_executions(self) -> int:
        with self._lock:
            return sum(
                1
                for job in self._jobs.values()
                if job.future is not None and not job.future.done()
            )

    @property
    def accepting(self) -> bool:
        with self._lock:
            return not self._shutdown_started

    def create_job(self, request: ReverseDesignTgRequest, runner: JobRunner) -> ReverseDesignTgJobStatusResponse:
        job = _ReverseDesignJob(job_id=uuid4().hex, request=request)
        with self._lock:
            if self._shutdown_started:
                raise ReverseDesignJobUnavailableError(
                    "Reverse-design job manager is shutting down"
                )
            self._jobs[job.job_id] = job
            try:
                future = self._executor.submit(self._run_job, job.job_id, runner)
            except Exception as exc:
                if self._jobs.get(job.job_id) is job:
                    self._jobs.pop(job.job_id, None)
                raise ReverseDesignJobUnavailableError(
                    "Reverse-design executor rejected the job"
                ) from exc
            job.future = future
            snapshot = self._snapshot(job)

        future.add_done_callback(
            lambda completed_future, submitted_job_id=job.job_id: self._on_future_done(
                submitted_job_id,
                completed_future,
            )
        )
        return snapshot

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
            if job.status in TerminalStatus:
                return self._snapshot(job)
            job.cancel_event.set()
            future = job.future
        if future is not None:
            future.cancel()
        return self.get_job(job_id)

    def wait_for_job(self, job_id: str, timeout: float | None = None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            future = job.future
        if future is not None:
            future.result(timeout=timeout)

    def stop_accepting(self) -> None:
        with self._lock:
            self._shutdown_started = True

    def cancel_pending(self) -> tuple[Future[None], ...]:
        with self._lock:
            active_jobs = [
                job for job in self._jobs.values() if job.status not in TerminalStatus
            ]
            for job in active_jobs:
                job.cancel_event.set()
            futures = tuple(
                job.future for job in active_jobs if job.future is not None
            )
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

    def shutdown(
        self,
        *,
        wait: bool = False,
        timeout_seconds: float | None = None,
    ) -> bool:
        self.stop_accepting()
        futures = self.cancel_pending()
        completed = self.wait_for_futures(
            futures,
            timeout_seconds=timeout_seconds,
        ) if wait else not futures
        self.close_executor(wait=wait and completed)
        return completed

    def _run_job(self, job_id: str, runner: JobRunner) -> None:
        if not self._mark_running(job_id):
            return
        try:
            response = runner(
                lambda **progress: self._update_progress(job_id, **progress),
                lambda: self._is_cancelled(job_id),
            )
        except Exception as exc:  # pragma: no cover - exercised through API/runtime failures
            if self._is_cancelled(job_id):
                self._mark_cancelled(job_id, "逆向设计搜索已取消。")
            else:
                self._mark_failed(job_id, str(exc))
            return

        with self._lock:
            job = self._jobs[job_id]
            if job.cancel_event.is_set():
                job.status = "cancelled"
                job.message = "逆向设计搜索已取消。"
            elif response.total >= job.request.candidate_size:
                job.status = "found_enough"
                job.message = f"已找到 {job.request.candidate_size} 个满足阈值的候选。"
            else:
                job.status = "exhausted"
                job.message = f"PI 数据库已扫描完成，未找到 {job.request.candidate_size} 个满足阈值的候选。"
            job.result = response
            job.matched_count = response.candidate_pool_size
            job.updated_at = _utc_now()
            job.finished_at = job.updated_at

    def _mark_running(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs[job_id]
            if job.status != "pending":
                return False
            if job.cancel_event.is_set():
                self._cancel_job_locked(job, "逆向设计搜索已取消。")
                return False
            job.status = "running"
            job.started_at = _utc_now()
            job.updated_at = job.started_at
            return True

    def _mark_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.status in TerminalStatus:
                return
            job.status = "failed"
            job.error = str(error)[:2000]
            job.message = None
            job.updated_at = _utc_now()
            job.finished_at = job.updated_at

    def _mark_cancelled(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.status in TerminalStatus:
                return
            self._cancel_job_locked(job, message)

    @staticmethod
    def _cancel_job_locked(job: _ReverseDesignJob, message: str) -> None:
        job.status = "cancelled"
        job.message = str(message)[:2000]
        job.error = None
        job.updated_at = _utc_now()
        job.finished_at = job.updated_at

    def _on_future_done(self, job_id: str, future: Future[None]) -> None:
        if future.cancelled():
            self._mark_cancelled(job_id, "逆向设计搜索在执行前被取消。")
            return
        unexpected_error = future.exception()
        if unexpected_error is not None:
            self._mark_failed(
                job_id,
                f"Reverse-design task terminated unexpectedly: {unexpected_error}",
            )

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
