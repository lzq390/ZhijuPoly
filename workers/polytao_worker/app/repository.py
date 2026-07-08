from __future__ import annotations

import logging
from typing import Any

from .config import WorkerSettings

try:
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - dependency is installed in the worker image.
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class PostgresJobRepository:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings

    def update_status(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        returned_count: int | None = None,
        progress_percent: int | None = None,
        progress_stage: str | None = None,
        progress_message: str | None = None,
    ) -> int:
        if not self._settings.db_configured:
            return 0
        if psycopg is None or sql is None or Jsonb is None:
            raise RuntimeError("psycopg is required when APP_POSTGRES_DSN is configured")

        query, params = self._build_update(
            job_id,
            status,
            result,
            error,
            returned_count,
            progress_percent,
            progress_stage,
            progress_message,
        )
        with psycopg.connect(self._settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.rowcount or 0

    def _build_update(
        self,
        job_id: str,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
        returned_count: int | None,
        progress_percent: int | None,
        progress_stage: str | None,
        progress_message: str | None,
    ) -> tuple[Any, list[Any]]:
        assignments: list[Any] = []
        params: list[Any] = []

        def assign(column: str, value: Any) -> None:
            assignments.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
            params.append(value)

        assign("status", status)
        assign("error_message", error)
        assign("worker_id", self._settings.worker_id)
        assign("worker_job_id", job_id)
        assign("worker_version", self._settings.worker_version)
        assign("engine", "polytao")
        if result is not None:
            assign("result_data", Jsonb(result))
        if returned_count is not None:
            assign("returned_count", returned_count)
        if progress_percent is not None:
            assign("progress_percent", progress_percent)
        if progress_stage is not None:
            assign("progress_stage", progress_stage)
        if progress_message is not None:
            assign("progress_message", progress_message)

        if status == "running":
            assignments.append(sql.SQL("attempts = attempts + 1"))
            assignments.append(sql.SQL("started_at = COALESCE(started_at, now())"))
        if status in {"completed", "failed"}:
            assignments.append(sql.SQL("finished_at = now()"))
        assignments.append(sql.SQL("updated_at = now()"))

        params.append(job_id)
        query = sql.SQL(
            """
            UPDATE generation.polytao_jobs
            SET {}
            WHERE job_id = %s
              AND status NOT IN ('completed', 'failed', 'cancelled')
            """
        ).format(sql.SQL(", ").join(assignments))
        return query, params
