from __future__ import annotations

import logging
from enum import Enum
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


class JobUpdateResult(str, Enum):
    UPDATED = "updated"
    ALREADY_TERMINAL = "already_terminal"
    MISSING = "missing"


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
        artifact_manifest: dict[str, Any] | None = None,
        result_summary: dict[str, Any] | None = None,
        byteff2_git_sha: str | None = None,
        gpu_device: str | None = None,
        error_category: str | None = None,
        worker_instance_id: str | None = None,
    ) -> JobUpdateResult:
        if not self._settings.db_configured:
            return JobUpdateResult.MISSING
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
            artifact_manifest,
            result_summary,
            byteff2_git_sha,
            gpu_device,
            error_category,
            worker_instance_id,
        )
        with psycopg.connect(self._settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if cur.fetchone() is not None:
                    return JobUpdateResult.UPDATED
                cur.execute(
                    sql.SQL("SELECT {} FROM {} WHERE {} = %s").format(
                        sql.Identifier(self._settings.status_column),
                        _qualified_identifier(self._settings.job_table),
                        sql.Identifier(self._settings.job_id_column),
                    ),
                    (job_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JobUpdateResult.MISSING
                current_status = row[0] if not isinstance(row, dict) else row[self._settings.status_column]
                if current_status in {"completed", "failed", "cancelled"}:
                    return JobUpdateResult.ALREADY_TERMINAL
                logger.error(
                    "monomer MD job update matched no row while status remained active: %s (%s)",
                    job_id,
                    current_status,
                )
                return JobUpdateResult.MISSING

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
        artifact_manifest: dict[str, Any] | None,
        result_summary: dict[str, Any] | None,
        byteff2_git_sha: str | None,
        gpu_device: str | None,
        error_category: str | None,
        worker_instance_id: str | None,
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
        if worker_instance_id is not None:
            assign(settings.worker_instance_id_column, worker_instance_id)
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
        if artifact_manifest is not None:
            assign(settings.artifact_manifest_column, Jsonb(artifact_manifest))
        if result_summary is not None:
            assign(settings.result_summary_column, Jsonb(result_summary))
        if byteff2_git_sha is not None:
            assign(settings.byteff2_git_sha_column, byteff2_git_sha)
        if gpu_device is not None:
            assign(settings.gpu_device_column, gpu_device)
        if error_category is not None:
            assign(settings.error_category_column, error_category)

        if status in {"submitted", "running"}:
            assignments.append(
                sql.SQL("{} = now()").format(sql.Identifier(settings.heartbeat_at_column))
            )
            assignments.append(
                sql.SQL("{} = now() + make_interval(secs => %s)").format(
                    sql.Identifier(settings.lease_expires_at_column)
                )
            )
            params.append(settings.lease_seconds)

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
                sql.SQL("{} = now()").format(sql.Identifier(settings.heartbeat_at_column))
            )
            assignments.append(
                sql.SQL("{} = now()").format(sql.Identifier(settings.lease_expires_at_column))
            )
        assignments.append(
            sql.SQL("{} = now()").format(sql.Identifier(settings.updated_at_column))
        )

        params.append(job_id)
        query = sql.SQL(
            "UPDATE {} SET {} WHERE {} = %s AND {} NOT IN ('completed', 'failed', 'cancelled') RETURNING {}"
        ).format(
            _qualified_identifier(settings.job_table),
            sql.SQL(", ").join(assignments),
            sql.Identifier(settings.job_id_column),
            sql.Identifier(settings.status_column),
            sql.Identifier(settings.status_column),
        )
        return query, params

    def heartbeat(self, job_ids: list[str], worker_instance_id: str) -> int:
        if not job_ids or not self._settings.db_configured:
            return 0
        if psycopg is None or sql is None:
            raise RuntimeError("psycopg is required when APP_POSTGRES_DSN is configured")
        settings = self._settings
        query = sql.SQL(
            "UPDATE {} SET {} = now(), {} = now() + make_interval(secs => %s), {} = now() "
            "WHERE {} = ANY(%s) AND {} = %s AND {} IN ('submitted', 'running')"
        ).format(
            _qualified_identifier(settings.job_table),
            sql.Identifier(settings.heartbeat_at_column),
            sql.Identifier(settings.lease_expires_at_column),
            sql.Identifier(settings.updated_at_column),
            sql.Identifier(settings.job_id_column),
            sql.Identifier(settings.worker_instance_id_column),
            sql.Identifier(settings.status_column),
        )
        with psycopg.connect(settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, (settings.lease_seconds, job_ids, worker_instance_id))
                return cur.rowcount or 0

    def reconcile_orphaned_jobs(self, worker_instance_id: str) -> int:
        if not self._settings.db_configured:
            return 0
        message = "Monomer MD worker was restarted before this job finished."
        return self._fail_jobs(
            worker_instance_id,
            message=message,
            error_category="worker_restarted",
            different_instance=True,
        )

    def fail_instance_jobs(
        self,
        worker_instance_id: str,
        *,
        message: str,
        error_category: str,
    ) -> int:
        if not self._settings.db_configured:
            return 0
        return self._fail_jobs(
            worker_instance_id,
            message=message,
            error_category=error_category,
            different_instance=False,
        )

    def _fail_jobs(
        self,
        worker_instance_id: str,
        *,
        message: str,
        error_category: str,
        different_instance: bool,
    ) -> int:
        if psycopg is None or sql is None:
            raise RuntimeError("psycopg is required when APP_POSTGRES_DSN is configured")
        settings = self._settings
        instance_predicate = sql.SQL("{} IS DISTINCT FROM %s") if different_instance else sql.SQL("{} = %s")
        query = sql.SQL(
            "UPDATE {} SET {} = 'failed', {} = 'failed', {} = %s, {} = %s, {} = %s, "
            "{} = now(), {} = now(), {} = now(), {} = now() "
            "WHERE {} = %s AND {} AND {} IN ('pending', 'submitted', 'running')"
        ).format(
            _qualified_identifier(settings.job_table),
            sql.Identifier(settings.status_column),
            sql.Identifier(settings.progress_stage_column),
            sql.Identifier(settings.progress_message_column),
            sql.Identifier(settings.error_column),
            sql.Identifier(settings.error_category_column),
            sql.Identifier(settings.heartbeat_at_column),
            sql.Identifier(settings.lease_expires_at_column),
            sql.Identifier(settings.updated_at_column),
            sql.Identifier(settings.finished_at_column),
            sql.Identifier(settings.worker_id_column),
            instance_predicate.format(sql.Identifier(settings.worker_instance_id_column)),
            sql.Identifier(settings.status_column),
        )
        params = (
            message,
            message,
            error_category,
            settings.worker_id,
            worker_instance_id,
        )
        with psycopg.connect(settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                return cur.rowcount or 0


def _qualified_identifier(value: str) -> Any:
    parts = [part.strip() for part in value.split(".") if part.strip()]
    if not parts:
        raise ValueError("table name must not be blank")
    return sql.SQL(".").join(sql.Identifier(part) for part in parts)
