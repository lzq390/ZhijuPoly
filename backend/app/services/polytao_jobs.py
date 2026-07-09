from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable

from app.postgres_database import postgres_connection
from app.services.polytao_repository import (
    mark_polytao_job_completed_postgres,
    mark_polytao_job_failed_postgres,
    mark_polytao_job_running_postgres,
    mark_polytao_job_submitted_postgres,
)
from app.services.polytao_runtime import PolytaoGenerationResult


JobRunner = Callable[[], PolytaoGenerationResult]


class PolytaoJobManager:
    def __init__(self, *, app_postgres_dsn: str, max_workers: int = 1) -> None:
        self._app_postgres_dsn = app_postgres_dsn
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_workers),
            thread_name_prefix="polytao-backend",
        )
        self._futures: dict[str, Future] = {}

    def submit_job(self, job_id: str, runner: JobRunner) -> None:
        with postgres_connection(self._app_postgres_dsn) as connection:
            mark_polytao_job_submitted_postgres(connection, job_id=job_id)
        future = self._executor.submit(self._run_job, job_id, runner)
        self._futures[job_id] = future
        future.add_done_callback(lambda _future, submitted_job_id=job_id: self._futures.pop(submitted_job_id, None))

    def shutdown(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=not wait)

    def _run_job(self, job_id: str, runner: JobRunner) -> None:
        with postgres_connection(self._app_postgres_dsn) as connection:
            mark_polytao_job_running_postgres(connection, job_id)
        try:
            result = runner()
        except Exception as exc:  # pragma: no cover - covered through API failure paths
            with postgres_connection(self._app_postgres_dsn) as connection:
                mark_polytao_job_failed_postgres(connection, job_id, str(exc))
            return

        with postgres_connection(self._app_postgres_dsn) as connection:
            mark_polytao_job_completed_postgres(
                connection,
                job_id=job_id,
                result=result.result,
                returned_count=result.returned_count,
            )
