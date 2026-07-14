from __future__ import annotations

import json
from datetime import datetime
from typing import Any


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
        WHERE status IN ('pending', 'submitted', 'running')
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
        WHERE status IN ('pending', 'submitted', 'running')
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
        WHERE status IN ('pending', 'submitted', 'running')
          AND run_mode = 'formal'
        """
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def mark_monomer_md_job_submitted_postgres(connection: Any, *, job_id: str, worker_id: str | None = None, worker_job_id: str | None = None, worker_version: str | None = None) -> None:
    connection.execute(
        """
        UPDATE md.monomer_md_jobs
        SET status = CASE WHEN status = 'pending' THEN 'submitted' ELSE status END,
            progress_stage = CASE WHEN status = 'pending' THEN 'submitted' ELSE progress_stage END,
            progress_message = CASE WHEN status = 'pending' THEN 'Submitted to the monomer MD worker.' ELSE progress_message END,
            worker_id = %s, worker_job_id = %s, worker_version = %s, updated_at = now()
        WHERE job_id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
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
        WHERE job_id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
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
        WHERE job_id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
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
          AND status NOT IN ('pending', 'submitted', 'running')
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
               created_at, updated_at, started_at, finished_at
        FROM md.monomer_md_jobs
        WHERE job_id = %s
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
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
