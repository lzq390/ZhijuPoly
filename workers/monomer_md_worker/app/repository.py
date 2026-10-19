from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from task_usage.ledger import metering_enabled, record_event
from task_usage import native_entitlements as ne

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
    CANCEL_REQUESTED = "cancel_requested"
    MISSING = "missing"


def _record_monomer_md_usage_row(connection: Any, row: Any) -> None:
    # Only immutable lifecycle fields enter the ledger; no request or worker secrets.
    status = {"pending": "queued", "submitted": "queued"}.get(row["status"], row["status"])
    if status == "cancel_requested":
        return  # A cancellation request is not proof of released resources.
    if status == "failed" and row["error_category"] == "worker_restarted":
        status = "interrupted"
    submitted_at = row["created_at"]
    started_at = row["started_at"] if status != "queued" else None
    finished_at = row["finished_at"] if status not in {"queued", "running"} else None
    occurred_at = submitted_at if status == "queued" else (started_at if status == "running" else finished_at)
    metrics = {}
    if started_at is not None:
        metrics["queue_ms"] = int((started_at - submitted_at).total_seconds() * 1000)
    if finished_at is not None:
        metrics["observed_wall_ms"] = int((finished_at - submitted_at).total_seconds() * 1000)
    task_id = f"monomer_md:{row['job_id']}"
    record_event(
        connection, owner_user_id=str(row["owner_user_id"]), module="monomer_md",
        task_id=task_id, attempt_id="1", event_key=f"{task_id}:1:{status}",
        status=status, occurred_at=occurred_at, submitted_at=submitted_at,
        started_at=started_at, finished_at=finished_at, metrics=metrics,
    )


def _md_completion_facts(row: dict[str, Any]) -> dict[str, bool] | None:
    # This hook runs only after the original worker's safe cleanup and its
    # identity-fenced completed UPDATE. Do not infer actual steps or equilibration
    # from completed_steps: the formal runner records an estimate there.
    result = row.get("result_data")
    report = isinstance(result, dict) and bool(result)
    if not report:
        return None  # Missing persisted evidence is unknown, not failed validation.
    formal_report = bool(report and result.get("run_mode") == "formal"
        and result.get("protocol") == row.get("protocol")
        and result.get("job_id") == row.get("job_id")
        and isinstance(result.get("metrics"), dict) and bool(result["metrics"])
        and isinstance(result.get("summary"), dict)
        and isinstance(result.get("result_file"), str) and bool(result["result_file"]))
    return {"validated": formal_report, "completion_proven": row.get("status") == "completed",
            "report_present": report, "protocol_completed": formal_report,
            "cleanup_confirmed": row.get("status") == "completed"}


class PostgresJobRepository:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings

    def _usage_columns(self) -> Any:
        settings = self._settings
        columns = (
            settings.job_id_column, "owner_user_id", settings.status_column,
            "created_at", settings.started_at_column, settings.finished_at_column,
            settings.error_category_column,
        )
        names = ("job_id", "owner_user_id", "status", "created_at", "started_at", "finished_at", "error_category")
        return sql.SQL(", ").join(
            sql.SQL("{} AS {}").format(sql.Identifier(column), sql.Identifier(name))
            for column, name in zip(columns, names)
        )

    @staticmethod
    def _usage_row(row: Any) -> dict[str, Any]:
        if isinstance(row, dict):
            return row
        return dict(zip(("job_id", "owner_user_id", "status", "created_at", "started_at", "finished_at", "error_category"), row))

    def _record_usage(self, connection: Any, cursor: Any, job_id: str) -> None:
        cursor.execute(
            sql.SQL("SELECT {} FROM {} WHERE {} = %s").format(
                self._usage_columns(), _qualified_identifier(self._settings.job_table),
                sql.Identifier(self._settings.job_id_column),
            ),
            (job_id,),
        )
        row = cursor.fetchone()
        if row is not None:
            _record_monomer_md_usage_row(connection, self._usage_row(row))

    def _settle_entitlement(self, connection: Any, cursor: Any, job_id: str) -> None:
        settings = self._settings
        columns = (settings.job_id_column, "owner_user_id", settings.status_column,
                   settings.result_column, settings.protocol_column, settings.run_mode_column,
                   settings.error_category_column)
        names = ("job_id", "owner_user_id", "status", "result_data", "protocol", "run_mode", "error_category")
        cursor.execute(sql.SQL("SELECT {} FROM {} WHERE {} = %s").format(
            sql.SQL(", ").join(sql.SQL("{} AS {}").format(sql.Identifier(column), sql.Identifier(name))
                               for column, name in zip(columns, names)),
            _qualified_identifier(settings.job_table), sql.Identifier(settings.job_id_column)), (job_id,))
        selected = cursor.fetchone()
        if selected is None:
            return
        row = selected if isinstance(selected, dict) else dict(zip(names, selected))
        common = {"owner_user_id": str(row["owner_user_id"]), "module": "monomer_md", "native_task_id": str(job_id)}
        if row["status"] == "completed":
            ne.settle_native(connection, **common, status="completed", facts=_md_completion_facts(row))
        elif row["error_category"] in {"resource_cleanup_unconfirmed", "cancel_cleanup_unconfirmed"}:
            ne.mark_native_unknown(connection, **common, reason="resource_cleanup_unconfirmed")
        else:
            ne.release_native(connection, **common, status=row["status"], source="md_worker_terminal")

    def authorize_start(self, job_id: str, *, worker_instance_id: str) -> bool:
        """Account then job lock matches administrative disable ordering."""
        from backend.app.task_observability import TaskExecutionContext, log_task_event
        if not self._settings.db_configured or psycopg is None:
            return False
        owner_id = None
        reason = "task_or_worker_invalid"

        def decide(cursor):
            nonlocal owner_id, reason
            cursor.execute("SELECT owner_user_id FROM md.monomer_md_jobs WHERE job_id = %s", (job_id,))
            owner = cursor.fetchone()
            if owner is None:
                return False
            owner_id = str(owner[0] if not isinstance(owner, dict) else owner["owner_user_id"])
            cursor.execute("SELECT status, is_system FROM auth.users WHERE user_id = %s FOR UPDATE", (owner_id,))
            account = cursor.fetchone()
            cursor.execute("SELECT status, start_authorized_at, worker_instance_id FROM md.monomer_md_jobs WHERE job_id = %s FOR UPDATE", (job_id,))
            job = cursor.fetchone()
            if job is None:
                return False
            values = list(job.values()) if isinstance(job, dict) else job
            if values[0] not in {"submitted", "running"} or values[2] != worker_instance_id:
                return False
            if values[1] is not None:
                # Same native job and worker instance already hold the original
                # authorization. This replays that grant, never a new execution.
                if ne.enabled() and not ne.claim_native_running(connection,
                        owner_user_id=owner_id, module="monomer_md", native_task_id=job_id,
                        executor_id=f"monomer_md:{job_id}", allow_authorized_replay=True)["allowed"]:
                    reason = "entitlement_not_authorized"
                    return False
                reason = "already_authorized"
                return True
            account_values = list(account.values()) if isinstance(account, dict) else account
            if account_values is None or (account_values[0] != "active" and not account_values[1]):
                reason = "account_disabled"
                return False
            # Preserve the original system-worker rule. Ordinary queued jobs
            # require membership at this first authorization boundary even when
            # count collection is disabled; existing native grants replay above.
            if not account_values[1] and ne.core.resolve_access_tier(connection, owner_user_id=owner_id) != "member":
                reason = "account_membership_required"
                return False
            if ne.enabled() and not ne.claim_native_running(connection,
                    owner_user_id=owner_id, module="monomer_md", native_task_id=job_id,
                    executor_id=f"monomer_md:{job_id}")["allowed"]:
                reason = "entitlement_not_authorized"
                return False
            reason = "authorized"
            cursor.execute("UPDATE md.monomer_md_jobs SET start_authorized_at = now() WHERE job_id = %s", (job_id,))
            return True

        with psycopg.connect(self._settings.app_postgres_dsn) as connection:
            with connection.cursor() as cursor:
                authorized = decide(cursor)
        if owner_id is not None:
            log_task_event(TaskExecutionContext(owner_id, None, "monomer_md", "md", job_id, worker_instance_id),
                           "start_authorized" if authorized else "start_rejected", reason=reason)
        return authorized

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
        allow_cancel_requested_failure: bool = False,
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
            allow_cancel_requested_failure,
        )
        with psycopg.connect(self._settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if cur.fetchone() is not None:
                    if metering_enabled():
                        self._record_usage(conn, cur, job_id)
                    if ne.enabled() and status in {"completed", "failed", "cancelled"}:
                        self._settle_entitlement(conn, cur, job_id)
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
                if current_status == "cancel_requested":
                    return JobUpdateResult.CANCEL_REQUESTED
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
        allow_cancel_requested_failure: bool,
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
        if status in {"completed", "failed", "cancelled"}:
            assignments.append(sql.SQL("{} = NULL").format(sql.Identifier("queue_sequence")))
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

        failed_statuses = (
            ("pending", "submitted", "running", "cancel_requested")
            if allow_cancel_requested_failure
            else ("pending", "submitted", "running")
        )
        allowed_statuses = {
            "submitted": ("pending", "submitted"),
            "running": ("submitted", "running"),
            "completed": ("running",),
            "failed": failed_statuses,
            "cancelled": ("pending", "submitted", "cancel_requested"),
            "cancel_requested": ("pending", "submitted", "running", "cancel_requested"),
        }.get(status, (status,))
        instance_fence = (
            sql.SQL(" AND {} = %s").format(sql.Identifier(settings.worker_instance_id_column))
            if worker_instance_id is not None else sql.SQL("")
        )
        query = sql.SQL(
            "UPDATE {} SET {} WHERE {} = %s AND {} = ANY(%s){} RETURNING {}"
        ).format(
            _qualified_identifier(settings.job_table),
            sql.SQL(", ").join(assignments),
            sql.Identifier(settings.job_id_column),
            sql.Identifier(settings.status_column),
            instance_fence,
            sql.Identifier(settings.status_column),
        )
        params.extend((job_id, list(allowed_statuses)))
        if worker_instance_id is not None:
            params.append(worker_instance_id)
        return query, params

    def accept_job(
        self,
        job_id: str,
        worker_instance_id: str,
        *,
        queued: bool,
    ) -> JobUpdateResult:
        if not self._settings.db_configured:
            return JobUpdateResult.UPDATED
        if psycopg is None or sql is None:
            raise RuntimeError("psycopg is required when APP_POSTGRES_DSN is configured")
        settings = self._settings
        queue_expression = (
            sql.SQL("nextval('md.monomer_md_queue_sequence_seq')")
            if queued
            else sql.SQL("NULL")
        )
        query = sql.SQL(
            "UPDATE {} SET {} = 'submitted', {} = %s, {} = %s, {} = %s, "
            "{} = %s, {} = %s, {} = {}, {} = now(), {} = now(), "
            "{} = now() + make_interval(secs => %s) "
            "WHERE {} = %s AND {} IN ('pending', 'submitted') RETURNING {}"
        ).format(
            _qualified_identifier(settings.job_table),
            sql.Identifier(settings.status_column),
            sql.Identifier(settings.worker_id_column),
            sql.Identifier(settings.worker_job_id_column),
            sql.Identifier(settings.worker_version_column),
            sql.Identifier(settings.worker_instance_id_column),
            sql.Identifier(settings.progress_stage_column),
            sql.Identifier("queue_sequence"),
            queue_expression,
            sql.Identifier(settings.updated_at_column),
            sql.Identifier(settings.heartbeat_at_column),
            sql.Identifier(settings.lease_expires_at_column),
            sql.Identifier(settings.job_id_column),
            sql.Identifier(settings.status_column),
            sql.Identifier(settings.status_column),
        )
        progress_stage = "queued" if queued else "acquiring_gpu"
        params = (
            settings.worker_id,
            job_id,
            settings.worker_version,
            worker_instance_id,
            progress_stage,
            settings.lease_seconds,
            job_id,
        )
        with psycopg.connect(settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                if cur.fetchone() is not None:
                    message = (
                        "Queued for the formal monomer MD execution slot."
                        if queued
                        else "Waiting to acquire GPU execution capacity."
                    )
                    cur.execute(
                        sql.SQL(
                            "UPDATE {} SET {} = %s WHERE {} = %s"
                        ).format(
                            _qualified_identifier(settings.job_table),
                            sql.Identifier(settings.progress_message_column),
                            sql.Identifier(settings.job_id_column),
                        ),
                        (message, job_id),
                    )
                    if metering_enabled():
                        self._record_usage(conn, cur, job_id)
                    return JobUpdateResult.UPDATED
        return self._current_update_result(job_id)

    def promote_queued_job(self, job_id: str) -> JobUpdateResult:
        if not self._settings.db_configured:
            return JobUpdateResult.UPDATED
        if psycopg is None or sql is None:
            raise RuntimeError("psycopg is required when APP_POSTGRES_DSN is configured")
        settings = self._settings
        query = sql.SQL(
            "UPDATE {} SET {} = NULL, {} = 'acquiring_gpu', {} = %s, {} = now() "
            "WHERE {} = %s AND {} = 'submitted' AND {} IS NOT NULL RETURNING {}"
        ).format(
            _qualified_identifier(settings.job_table),
            sql.Identifier("queue_sequence"),
            sql.Identifier(settings.progress_stage_column),
            sql.Identifier(settings.progress_message_column),
            sql.Identifier(settings.updated_at_column),
            sql.Identifier(settings.job_id_column),
            sql.Identifier(settings.status_column),
            sql.Identifier("queue_sequence"),
            sql.Identifier(settings.status_column),
        )
        with psycopg.connect(settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    ("Waiting to acquire GPU execution capacity.", job_id),
                )
                if cur.fetchone() is not None:
                    return JobUpdateResult.UPDATED
        return self._current_update_result(job_id)

    def _current_update_result(self, job_id: str) -> JobUpdateResult:
        if psycopg is None or sql is None:
            raise RuntimeError("psycopg is required when APP_POSTGRES_DSN is configured")
        settings = self._settings
        with psycopg.connect(settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL("SELECT {} FROM {} WHERE {} = %s").format(
                        sql.Identifier(settings.status_column),
                        _qualified_identifier(settings.job_table),
                        sql.Identifier(settings.job_id_column),
                    ),
                    (job_id,),
                )
                row = cur.fetchone()
        if row is None:
            return JobUpdateResult.MISSING
        current_status = row[0] if not isinstance(row, dict) else row[settings.status_column]
        if current_status in {"completed", "failed", "cancelled"}:
            return JobUpdateResult.ALREADY_TERMINAL
        if current_status == "cancel_requested":
            return JobUpdateResult.CANCEL_REQUESTED
        return JobUpdateResult.MISSING

    def heartbeat(self, job_ids: list[str], worker_instance_id: str) -> int:
        if not job_ids or not self._settings.db_configured:
            return 0
        if psycopg is None or sql is None:
            raise RuntimeError("psycopg is required when APP_POSTGRES_DSN is configured")
        settings = self._settings
        query = sql.SQL(
            "UPDATE {} SET {} = now(), {} = now() + make_interval(secs => %s), {} = now() "
            "WHERE {} = ANY(%s) AND {} = %s AND {} IN ('submitted', 'running', 'cancel_requested')"
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

    def recovery_job_ids(self) -> tuple[str, ...]:
        """Bounded exact-worker history, including terminal jobs with stale leases."""
        if not self._settings.acceptance_attachment_id:
            raise RuntimeError('Exact recovery inventory is acceptance-only')
        if psycopg is None or sql is None or not self._settings.db_configured:
            raise RuntimeError('Exact recovery inventory requires its application database')
        settings = self._settings
        query = sql.SQL('SELECT {} FROM {} WHERE {} = %s ORDER BY {} LIMIT 65').format(
            sql.Identifier(settings.job_id_column), _qualified_identifier(settings.job_table),
            sql.Identifier(settings.worker_id_column), sql.Identifier(settings.job_id_column))
        with psycopg.connect(settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, (settings.worker_id,))
                rows = cur.fetchall()
        if len(rows) > 64:
            raise RuntimeError('Exact recovery inventory exceeds the approved finite scope')
        return tuple(str(row[0]) for row in rows)

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

    def reconcile_cancel_requested_jobs(self) -> int:
        if not self._settings.db_configured:
            return 0
        if psycopg is None or sql is None:
            raise RuntimeError("psycopg is required when APP_POSTGRES_DSN is configured")
        settings = self._settings
        query = sql.SQL(
            "UPDATE {} SET {} = 'cancelled', {} = 'cancelled', {} = %s, "
            "{} = NULL, {} = now(), {} = now(), {} = now() "
            "WHERE {} = 'cancel_requested'"
        ).format(
            _qualified_identifier(settings.job_table),
            sql.Identifier(settings.status_column),
            sql.Identifier(settings.progress_stage_column),
            sql.Identifier(settings.progress_message_column),
            sql.Identifier("queue_sequence"),
            sql.Identifier(settings.updated_at_column),
            sql.Identifier(settings.finished_at_column),
            sql.Identifier(settings.heartbeat_at_column),
            sql.Identifier(settings.status_column),
        )
        parameters = ("Cancellation completed while recovering the monomer MD worker.",)
        if settings.acceptance_attachment_id:
            query += sql.SQL(' AND {} = %s').format(sql.Identifier(settings.worker_id_column))
            parameters += (settings.worker_id,)
        if metering_enabled() or ne.enabled():
            query += sql.SQL(" RETURNING ") + self._usage_columns()
        with psycopg.connect(settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    parameters,
                )
                count = cur.rowcount or 0
                if metering_enabled() or ne.enabled():
                    for selected in cur.fetchall():
                        row = self._usage_row(selected)
                        if metering_enabled():
                            _record_monomer_md_usage_row(conn, row)
                        if ne.enabled():
                            # Caller already proved original native recovery safe;
                            # this adds no process-death or lease-expiry inference.
                            ne.release_native(conn, owner_user_id=str(row["owner_user_id"]),
                                module="monomer_md", native_task_id=str(row["job_id"]),
                                status=row["status"], source="md_native_recovery")
                return count

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
        if metering_enabled() or ne.enabled():
            query += sql.SQL(" RETURNING ") + self._usage_columns()
        with psycopg.connect(settings.app_postgres_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                count = cur.rowcount or 0
                if metering_enabled() or ne.enabled():
                    for selected in cur.fetchall():
                        row = self._usage_row(selected)
                        if metering_enabled():
                            _record_monomer_md_usage_row(conn, row)
                        if ne.enabled():
                            # Caller already proved original native recovery safe;
                            # this adds no process-death or lease-expiry inference.
                            ne.release_native(conn, owner_user_id=str(row["owner_user_id"]),
                                module="monomer_md", native_task_id=str(row["job_id"]),
                                status=row["status"], source="md_native_recovery")
                return count


def _qualified_identifier(value: str) -> Any:
    parts = [part.strip() for part in value.split(".") if part.strip()]
    if not parts:
        raise ValueError("table name must not be blank")
    return sql.SQL(".").join(sql.Identifier(part) for part in parts)
