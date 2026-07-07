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
        output_dir: str | None = None,
        artifacts: dict[str, Any] | None = None,
        completed_steps: int | None = None,
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
            output_dir,
            artifacts,
            completed_steps,
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
        output_dir: str | None,
        artifacts: dict[str, Any] | None,
        completed_steps: int | None,
        progress_percent: int | None,
        progress_stage: str | None,
        progress_message: str | None,
    ) -> tuple[Any, list[Any]]:
        settings = self._settings
        assignments: list[Any] = []
        params: list[Any] = []

        def assign(column: str, value: Any) -> None:
            assignments.append(sql.SQL("{} = %s").format(sql.Identifier(column)))
            params.append(value)

        assign(settings.status_column, status)
        assign(settings.error_column, error)
        assign(settings.worker_id_column, settings.worker_id)
        assign(settings.worker_job_id_column, job_id)
        assign(settings.worker_version_column, settings.worker_version)
        if result is not None:
            assign(settings.result_column, Jsonb(result))
        if output_dir is not None:
            assign(settings.output_dir_column, output_dir)
        if artifacts is not None:
            assign(settings.artifacts_column, Jsonb(artifacts))
        if completed_steps is not None:
            assign(settings.completed_steps_column, completed_steps)
        if progress_percent is not None:
            assign(settings.progress_percent_column, progress_percent)
        if progress_stage is not None:
            assign(settings.progress_stage_column, progress_stage)
        if progress_message is not None:
            assign(settings.progress_message_column, progress_message)

        if status == "running":
            assignments.append(
                sql.SQL("{} = COALESCE({}, now())").format(
                    sql.Identifier(settings.started_at_column),
                    sql.Identifier(settings.started_at_column),
                )
            )
        if status in {"completed", "failed"}:
            assignments.append(
                sql.SQL("{} = now()").format(sql.Identifier(settings.finished_at_column))
            )
        assignments.append(
            sql.SQL("{} = now()").format(sql.Identifier(settings.updated_at_column))
        )

        params.append(job_id)
        query = sql.SQL(
            "UPDATE {} SET {} WHERE {} = %s AND {} NOT IN ('completed', 'failed', 'cancelled')"
        ).format(
            _qualified_identifier(settings.job_table),
            sql.SQL(", ").join(assignments),
            sql.Identifier(settings.job_id_column),
            sql.Identifier(settings.status_column),
        )
        return query, params


def _qualified_identifier(value: str) -> Any:
    parts = [part.strip() for part in value.split(".") if part.strip()]
    if not parts:
        raise ValueError("table name must not be blank")
    return sql.SQL(".").join(sql.Identifier(part) for part in parts)
