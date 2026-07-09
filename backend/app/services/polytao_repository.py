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


def create_polytao_job_postgres(
    connection: Any,
    *,
    job_id: str,
    input_smiles: str | None,
    canonical_smiles: str | None,
    descriptor_prompt: str,
    descriptors: dict[str, float],
    request_data: dict[str, Any],
    requested_count: int,
) -> None:
    connection.execute(
        """
        INSERT INTO generation.polytao_jobs (
          job_id, status, input_smiles, canonical_smiles, descriptor_prompt, descriptors,
          request_data, requested_count, progress_stage, progress_message, engine
        )
        VALUES (
          %s, 'pending', %s, %s, %s, %s::jsonb, %s::jsonb, %s,
          'pending', 'Waiting for the PolyTAO backend runtime to start.', 'polytao-backend'
        )
        """,
        (
            job_id,
            input_smiles,
            canonical_smiles,
            descriptor_prompt,
            _jsonb(descriptors),
            _jsonb(request_data),
            requested_count,
        ),
    )


def count_active_polytao_jobs_postgres(connection: Any) -> int:
    row = connection.execute(
        """
        SELECT count(*) AS count
        FROM generation.polytao_jobs
        WHERE status IN ('pending', 'submitted', 'running')
        """
    ).fetchone()
    return int(row["count"] if row is not None else 0)


def mark_polytao_job_submitted_postgres(
    connection: Any,
    *,
    job_id: str,
    worker_id: str | None = None,
    worker_job_id: str | None = None,
    worker_version: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE generation.polytao_jobs
        SET status = CASE WHEN status = 'pending' THEN 'submitted' ELSE status END,
            progress_stage = CASE WHEN status = 'pending' THEN 'submitted' ELSE progress_stage END,
            progress_message = CASE WHEN status = 'pending' THEN 'Submitted to the PolyTAO backend runtime.' ELSE progress_message END,
            worker_id = %s,
            worker_job_id = %s,
            worker_version = %s,
            engine = 'polytao-backend',
            updated_at = now()
        WHERE job_id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        (worker_id, worker_job_id, worker_version, job_id),
    )


def mark_polytao_job_running_postgres(connection: Any, job_id: str) -> None:
    connection.execute(
        """
        UPDATE generation.polytao_jobs
        SET status = 'running',
            attempts = attempts + 1,
            progress_percent = 10,
            progress_stage = 'running',
            progress_message = 'Running PolyTAO generation in the backend runtime.',
            engine = 'polytao-backend',
            updated_at = now(),
            started_at = COALESCE(started_at, now())
        WHERE job_id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        (job_id,),
    )


def mark_polytao_job_completed_postgres(
    connection: Any,
    *,
    job_id: str,
    result: dict[str, Any],
    returned_count: int,
) -> None:
    connection.execute(
        """
        UPDATE generation.polytao_jobs
        SET status = 'completed',
            result_data = %s::jsonb,
            returned_count = %s,
            progress_percent = 100,
            progress_stage = 'completed',
            progress_message = 'PolyTAO generation completed in the backend runtime.',
            engine = 'polytao-backend',
            updated_at = now(),
            finished_at = now()
        WHERE job_id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        (_jsonb(result), returned_count, job_id),
    )


def mark_polytao_job_failed_postgres(connection: Any, job_id: str, error_message: str) -> None:
    connection.execute(
        """
        UPDATE generation.polytao_jobs
        SET status = 'failed',
            progress_stage = 'failed',
            progress_message = %s,
            error_message = %s,
            engine = 'polytao-backend',
            updated_at = now(),
            finished_at = now()
        WHERE job_id = %s AND status NOT IN ('completed', 'failed', 'cancelled')
        """,
        (error_message, error_message, job_id),
    )


def mark_stale_polytao_jobs_failed_postgres(connection: Any) -> None:
    message = "PolyTAO backend restarted before this job finished."
    connection.execute(
        """
        UPDATE generation.polytao_jobs
        SET status = 'failed',
            progress_stage = 'failed',
            progress_message = %s,
            error_message = %s,
            engine = 'polytao-backend',
            updated_at = now(),
            finished_at = now()
        WHERE status IN ('pending', 'submitted', 'running')
        """,
        (message, message),
    )


def get_polytao_job_postgres(connection: Any, job_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT job_id, status, input_smiles, canonical_smiles, descriptor_prompt, descriptors,
               request_data, requested_count, returned_count, attempts, progress_percent,
               progress_stage, progress_message, worker_id, worker_job_id, worker_version,
               engine, result_data, error_message, created_at, updated_at, started_at, finished_at
        FROM generation.polytao_jobs
        WHERE job_id = %s
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "input_smiles": row["input_smiles"],
        "canonical_smiles": row["canonical_smiles"],
        "prompt": row["descriptor_prompt"],
        "requested_count": int(row["requested_count"]),
        "returned_count": int(row["returned_count"]),
        "attempts": int(row["attempts"]),
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
        "error_message": row["error_message"],
        "result": _as_dict(row["result_data"]) or None,
    }
