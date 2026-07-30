from __future__ import annotations

import json
from datetime import datetime
from typing import Any


MONOMER_MD_ACTIVE_STATUSES = frozenset(
    {"pending", "submitted", "running", "cancel_requested"}
)
MONOMER_MD_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _jsonb(value: dict[str, Any] | None) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def create_monomer_md_job_postgres(
    connection: Any,
    *,
    job_id: str,
    input_smiles: str,
    canonical_smiles: str,
    requested_steps: int,
    protocol: str = "DensityDemo",
    run_mode: str = "demo",
    config_json: dict[str, Any] | None = None,
    components: dict[str, Any] | None = None,
    pending_lease_seconds: int = 120,
) -> None:
    connection.execute(
        """
        INSERT INTO md.monomer_md_jobs (
          job_id, status, input_smiles, canonical_smiles, requested_steps, progress_stage, progress_message,
          protocol, run_mode, config_json, components, engine, lease_expires_at
        )
        VALUES (%s, 'pending', %s, %s, %s, 'pending', 'Waiting for the monomer MD worker to start.',
                %s, %s, %s::jsonb, %s::jsonb,
                CASE WHEN %s = 'formal' THEN 'byteff2-formal-worker' ELSE 'byteff2-density-demo-worker' END,
                now() + make_interval(secs => %s))
        """,
        (
            job_id,
            input_smiles,
            canonical_smiles,
            requested_steps,
            protocol,
            run_mode,
            _jsonb(config_json),
            _jsonb(components),
            run_mode,
            pending_lease_seconds,
        ),
    )


def mark_expired_unclaimed_monomer_md_jobs_failed_postgres(connection: Any) -> int:
    message = "Monomer MD job was not claimed by the worker before its submission lease expired."
    cursor = connection.execute(
        """
        UPDATE md.monomer_md_jobs
        SET status = 'failed', progress_stage = 'failed', progress_message = %s,
            error_message = %s, error_category = 'submit_lease_expired',
            updated_at = now(), finished_at = now()
        WHERE status = 'pending'
          AND worker_instance_id IS NULL
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at < now()
        """,
        (message, message),
    )
    return int(cursor.rowcount or 0)


def count_active_monomer_md_jobs_postgres(connection: Any) -> int:
    row = connection.execute(
        """
        SELECT count(*) AS count
        FROM md.monomer_md_jobs
        WHERE status IN ('pending', 'submitted', 'running', 'cancel_requested')
        """
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def get_active_monomer_md_capacity_postgres(connection: Any) -> tuple[int, int | None]:
    row = connection.execute(
        """
        SELECT count(*) AS count,
               floor(extract(epoch FROM now() - min(COALESCE(heartbeat_at, updated_at))))::bigint
                 AS oldest_heartbeat_age_seconds
        FROM md.monomer_md_jobs
        WHERE status IN ('pending', 'submitted', 'running', 'cancel_requested')
        """
    ).fetchone()
    if row is None:
        return 0, None
    age = row["oldest_heartbeat_age_seconds"]
    return int(row["count"]), max(0, int(age)) if age is not None else None


def reconcile_and_get_active_monomer_md_capacity_postgres(
    connection: Any,
    *,
    advisory_lock_id: int,
) -> tuple[int, int | None]:
    """Converge expired unclaimed jobs and count capacity under one transaction lock."""
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (advisory_lock_id,))
    mark_expired_unclaimed_monomer_md_jobs_failed_postgres(connection)
    return get_active_monomer_md_capacity_postgres(connection)


def count_active_formal_monomer_md_jobs_postgres(connection: Any) -> int:
    row = connection.execute(
        """
        SELECT count(*) AS count
        FROM md.monomer_md_jobs
        WHERE status IN ('pending', 'submitted', 'running', 'cancel_requested')
          AND run_mode = 'formal'
        """
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def get_monomer_md_mode_capacity_postgres(connection: Any) -> dict[str, int]:
    row = connection.execute(
        """
        SELECT
          count(*) FILTER (
            WHERE run_mode = 'demo'
              AND status IN ('pending', 'submitted', 'running', 'cancel_requested')
          ) AS demo_active,
          count(*) FILTER (
            WHERE run_mode = 'formal'
              AND status IN ('pending', 'submitted', 'running', 'cancel_requested')
              AND queue_sequence IS NULL
          ) AS formal_running,
          count(*) FILTER (
            WHERE run_mode = 'formal'
              AND status IN ('pending', 'submitted', 'running', 'cancel_requested')
              AND queue_sequence IS NOT NULL
          ) AS formal_queued
        FROM md.monomer_md_jobs
        """
    ).fetchone()
    if row is None:
        return {"demo_active": 0, "formal_running": 0, "formal_queued": 0}
    return {
        "demo_active": int(row["demo_active"]),
        "formal_running": int(row["formal_running"]),
        "formal_queued": int(row["formal_queued"]),
    }


def mark_monomer_md_job_submitted_postgres(connection: Any, *, job_id: str, worker_id: str | None = None, worker_job_id: str | None = None, worker_version: str | None = None) -> None:
    connection.execute(
        """
        UPDATE md.monomer_md_jobs
        SET status = CASE WHEN status = 'pending' THEN 'submitted' ELSE status END,
            progress_stage = CASE WHEN status = 'pending' THEN 'submitted' ELSE progress_stage END,
            progress_message = CASE WHEN status = 'pending' THEN 'Submitted to the monomer MD worker.' ELSE progress_message END,
            worker_id = %s, worker_job_id = %s, worker_version = %s, updated_at = now()
        WHERE job_id = %s
          AND status NOT IN ('completed', 'failed', 'cancelled', 'cancel_requested')
        """,
        (worker_id, worker_job_id, worker_version, job_id),
    )


def mark_monomer_md_job_completed_postgres(
    connection: Any,
    *,
    job_id: str,
    result_data: dict[str, Any],
    artifacts: dict[str, Any] | None = None,
    artifact_root: str | None = None,
    completed_steps: int | None = None,
    artifact_manifest: dict[str, Any] | None = None,
    result_summary: dict[str, Any] | None = None,
    byteff2_git_sha: str | None = None,
    gpu_device: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE md.monomer_md_jobs
        SET status = 'completed', completed_steps = COALESCE(%s, requested_steps), progress_percent = 100,
            progress_stage = 'completed',
            progress_message = CASE WHEN run_mode = 'formal' THEN 'ByteFF2 formal protocol completed.' ELSE 'Monomer MD demo completed.' END,
            artifact_root = COALESCE(%s, artifact_root),
            artifacts = %s::jsonb, artifact_manifest = %s::jsonb, result_summary = %s::jsonb,
            result_data = %s::jsonb, byteff2_git_sha = COALESCE(%s, byteff2_git_sha), gpu_device = COALESCE(%s, gpu_device),
            error_message = NULL, error_category = NULL, updated_at = now(), finished_at = now()
        WHERE job_id = %s
          AND status NOT IN ('completed', 'failed', 'cancelled', 'cancel_requested')
        """,
        (
            completed_steps,
            artifact_root,
            _jsonb(artifacts),
            _jsonb(artifact_manifest),
            _jsonb(result_summary),
            _jsonb(result_data),
            byteff2_git_sha,
            gpu_device,
            job_id,
        ),
    )


def mark_monomer_md_job_failed_postgres(connection: Any, job_id: str, error_message: str, error_category: str | None = None) -> None:
    connection.execute(
        """
        UPDATE md.monomer_md_jobs
        SET status = 'failed', progress_stage = 'failed', progress_message = %s, error_message = %s,
            error_category = %s, updated_at = now(), finished_at = now()
        WHERE job_id = %s
          AND status NOT IN ('completed', 'failed', 'cancelled', 'cancel_requested')
        """,
        (error_message, error_message, error_category, job_id),
    )


def mark_monomer_md_artifacts_deleted_postgres(connection: Any, *, job_id: str, message: str) -> None:
    connection.execute(
        """
        UPDATE md.monomer_md_jobs
        SET artifact_deleted_at = COALESCE(artifact_deleted_at, now()),
            artifact_delete_message = %s,
            artifact_manifest = jsonb_set(
              COALESCE(artifact_manifest, '{}'::jsonb),
              '{deleted}',
              'true'::jsonb,
              true
            ),
            updated_at = now()
        WHERE job_id = %s
          AND status NOT IN ('pending', 'submitted', 'running', 'cancel_requested')
        """,
        (message, job_id),
    )


def get_monomer_md_job_postgres(connection: Any, job_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT job_id, status, input_smiles, canonical_smiles, protocol, run_mode, config_json, components,
               requested_steps, completed_steps, progress_percent,
               progress_stage, progress_message, worker_id, worker_job_id, worker_version, engine, artifact_root,
               artifacts, artifact_manifest, artifact_deleted_at, artifact_delete_message, result_summary,
               byteff2_git_sha, gpu_device, error_category, result_data, error_message,
               created_at, updated_at, started_at, finished_at, cancel_requested_at, queue_sequence,
               CASE
                 WHEN queue_sequence IS NULL THEN NULL
                 ELSE (
                   SELECT count(*) + 1
                   FROM md.monomer_md_jobs queued
                   WHERE queued.run_mode = 'formal'
                     AND queued.status IN ('pending', 'submitted', 'running', 'cancel_requested')
                     AND queued.queue_sequence IS NOT NULL
                     AND queued.queue_sequence < job.queue_sequence
                 )
               END AS queue_position
        FROM md.monomer_md_jobs job
        WHERE job_id = %s
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return _monomer_md_job_from_row(row)


def list_expired_monomer_md_jobs_postgres(
    connection: Any,
    *,
    retention_days: int,
    limit: int,
    after_terminal_at: datetime | None = None,
    after_job_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return terminal retention candidates in a stable keyset order."""
    cursor_sql = ""
    params: list[Any] = [retention_days, retention_days]
    if after_terminal_at is not None and after_job_id is not None:
        cursor_sql = """
          AND (COALESCE(finished_at, updated_at), job_id) > (%s, %s)
        """
        params.extend((after_terminal_at, after_job_id))
    params.append(limit)
    rows = connection.execute(
        f"""
        SELECT job_id, status, finished_at, updated_at,
               COALESCE(finished_at, updated_at) AS terminal_at
        FROM md.monomer_md_jobs
        WHERE status IN ('completed', 'failed', 'cancelled')
          AND created_at <= now() - (%s * interval '1 day')
          AND COALESCE(finished_at, updated_at) <=
              now() - (%s * interval '1 day')
          {cursor_sql}
        ORDER BY COALESCE(finished_at, updated_at), job_id
        LIMIT %s
        """,
        tuple(params),
    ).fetchall()
    return [
        {
            "job_id": str(row["job_id"]),
            "status": str(row["status"]),
            "finished_at": row["finished_at"],
            "updated_at": row["updated_at"],
            "terminal_at": row["terminal_at"],
        }
        for row in rows
    ]


def delete_monomer_md_job_cas_postgres(
    connection: Any,
    *,
    job_id: str,
    expected_status: str,
    expected_finished_at: Any,
    expected_updated_at: Any,
) -> bool:
    """Delete only the exact terminal record that was cleared by the Worker."""
    row = connection.execute(
        """
        DELETE FROM md.monomer_md_jobs
        WHERE job_id = %s
          AND status = %s
          AND status IN ('completed', 'failed', 'cancelled')
          AND finished_at IS NOT DISTINCT FROM %s
          AND updated_at IS NOT DISTINCT FROM %s
        RETURNING job_id
        """,
        (
            job_id,
            expected_status,
            expected_finished_at,
            expected_updated_at,
        ),
    ).fetchone()
    return row is not None


def list_monomer_md_jobs_postgres(
    connection: Any,
    *,
    run_mode: str | None = None,
    active_only: bool = False,
    protocol: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    where_parts = ["TRUE"]
    params: list[Any] = []
    if run_mode is not None:
        where_parts.append("job.run_mode = %s")
        params.append(run_mode)
    if protocol is not None:
        where_parts.append("job.protocol = %s")
        params.append(protocol)
    if status is not None:
        where_parts.append("job.status = %s")
        params.append(status)
    if active_only:
        where_parts.append("job.status IN ('pending', 'submitted', 'running', 'cancel_requested')")
    where_sql = " AND ".join(where_parts)
    total_row = connection.execute(
        f"SELECT count(*) AS count FROM md.monomer_md_jobs job WHERE {where_sql}",
        tuple(params),
    ).fetchone()
    total = int(total_row["count"] if total_row is not None else 0)
    rows = connection.execute(
        f"""
        SELECT job_id, status, input_smiles, canonical_smiles, protocol, run_mode, config_json, components,
               requested_steps, completed_steps, progress_percent,
               progress_stage, progress_message, worker_id, worker_job_id, worker_version, engine, artifact_root,
               artifacts, artifact_manifest, artifact_deleted_at, artifact_delete_message, result_summary,
               byteff2_git_sha, gpu_device, error_category, result_data, error_message,
               created_at, updated_at, started_at, finished_at, cancel_requested_at, queue_sequence,
               CASE
                 WHEN queue_sequence IS NULL THEN NULL
                 ELSE (
                   SELECT count(*) + 1
                   FROM md.monomer_md_jobs queued
                   WHERE queued.run_mode = 'formal'
                     AND queued.status IN ('pending', 'submitted', 'running', 'cancel_requested')
                     AND queued.queue_sequence IS NOT NULL
                     AND queued.queue_sequence < job.queue_sequence
                 )
               END AS queue_position
        FROM md.monomer_md_jobs job
        WHERE {where_sql}
        ORDER BY
          CASE WHEN %s AND job.status IN ('pending', 'submitted', 'running', 'cancel_requested')
               THEN CASE WHEN job.queue_sequence IS NULL THEN 0 ELSE 1 END
               ELSE 2 END,
          CASE WHEN %s THEN job.queue_sequence END NULLS FIRST,
          job.created_at DESC,
          job.job_id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(params + [active_only, active_only, page_size, (page - 1) * page_size]),
    ).fetchall()
    return [_monomer_md_job_from_row(row) for row in rows], total


def request_monomer_md_job_cancel_postgres(
    connection: Any,
    *,
    job_id: str,
) -> tuple[dict[str, Any] | None, bool]:
    cursor = connection.execute(
        """
        UPDATE md.monomer_md_jobs
        SET status = 'cancel_requested',
            cancel_requested_at = COALESCE(cancel_requested_at, now()),
            progress_stage = 'cancel_requested',
            progress_message = 'Cancellation requested; waiting for worker cleanup.',
            updated_at = now()
        WHERE job_id = %s
          AND status IN ('submitted', 'running')
        RETURNING job_id
        """,
        (job_id,),
    )
    changed = cursor.fetchone() is not None
    return get_monomer_md_job_postgres(connection, job_id), changed


def _monomer_md_job_from_row(row: Any) -> dict[str, Any]:
    result_data = _as_dict(row["result_data"])
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "input_smiles": row["input_smiles"],
        "canonical_smiles": row["canonical_smiles"],
        "protocol": row["protocol"],
        "run_mode": row["run_mode"],
        "config_json": _as_dict(row["config_json"]),
        "components": _as_dict(row["components"]),
        "requested_steps": int(row["requested_steps"]),
        "completed_steps": int(row["completed_steps"]),
        "progress_percent": int(row["progress_percent"]),
        "progress_stage": row["progress_stage"],
        "progress_message": row["progress_message"],
        "queue_position": int(row["queue_position"]) if row["queue_position"] is not None else None,
        "cancel_requested_at": _timestamp(row["cancel_requested_at"]),
        "created_at": _timestamp(row["created_at"]),
        "updated_at": _timestamp(row["updated_at"]),
        "started_at": _timestamp(row["started_at"]),
        "finished_at": _timestamp(row["finished_at"]),
        "worker_id": row["worker_id"],
        "worker_job_id": row["worker_job_id"],
        "worker_version": row["worker_version"],
        "engine": row["engine"],
        "artifact_root": row["artifact_root"],
        "artifacts": _as_dict(row["artifacts"]),
        "artifact_manifest": _as_dict(row["artifact_manifest"]),
        "artifact_deleted_at": _timestamp(row["artifact_deleted_at"]),
        "artifact_delete_message": row["artifact_delete_message"],
        "result_summary": _as_dict(row["result_summary"]),
        "byteff2_git_sha": row["byteff2_git_sha"],
        "gpu_device": row["gpu_device"],
        "error_category": row["error_category"],
        "error_message": row["error_message"],
        "result": result_data or None,
    }
