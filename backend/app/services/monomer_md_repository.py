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


def create_monomer_md_job_postgres(connection: Any, *, job_id: str, input_smiles: str, canonical_smiles: str, requested_steps: int) -> None:
    connection.execute(
        """
        INSERT INTO md.monomer_md_jobs (
          job_id, status, input_smiles, canonical_smiles, requested_steps, progress_stage, progress_message
        )
        VALUES (%s, 'pending', %s, %s, %s, 'pending', 'Waiting for the monomer MD worker to start.')
        """,
        (job_id, input_smiles, canonical_smiles, requested_steps),
    )


def count_active_monomer_md_jobs_postgres(connection: Any) -> int:
    row = connection.execute(
        """
        SELECT count(*) AS count
        FROM md.monomer_md_jobs
        WHERE status IN ('pending', 'submitted', 'running')
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


def mark_monomer_md_job_completed_postgres(connection: Any, *, job_id: str, result_data: dict[str, Any], artifacts: dict[str, Any] | None = None, artifact_root: str | None = None, completed_steps: int | None = None) -> None:
    connection.execute(
        """
        UPDATE md.monomer_md_jobs
        SET status = 'completed', completed_steps = COALESCE(%s, requested_steps), progress_percent = 100,
            progress_stage = 'completed', progress_message = 'Monomer MD demo completed.', artifact_root = COALESCE(%s, artifact_root),
            artifacts = %s::jsonb, result_data = %s::jsonb, error_message = NULL, updated_at = now(), finished_at = now()
        WHERE job_id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        (completed_steps, artifact_root, _jsonb(artifacts), _jsonb(result_data), job_id),
    )


def mark_monomer_md_job_failed_postgres(connection: Any, job_id: str, error_message: str) -> None:
    connection.execute(
        """
        UPDATE md.monomer_md_jobs
        SET status = 'failed', progress_stage = 'failed', progress_message = %s, error_message = %s, updated_at = now(), finished_at = now()
        WHERE job_id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        (error_message, error_message, job_id),
    )


def get_monomer_md_job_postgres(connection: Any, job_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT job_id, status, input_smiles, canonical_smiles, requested_steps, completed_steps, progress_percent,
               progress_stage, progress_message, worker_id, worker_job_id, worker_version, engine, artifact_root,
               artifacts, result_data, error_message, created_at, updated_at, started_at, finished_at
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
        "error_message": row["error_message"],
        "result": result_data or None,
    }
