from __future__ import annotations

import json
from datetime import datetime
from typing import Any


def _jsonb(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _result_data(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    return dict(value)


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def save_online_history_postgres(
    connection: Any,
    *,
    material: str,
    mode: str,
    max_papers: int,
    result_data: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO online_knowledge.history (
          material,
          mode,
          created_at,
          papers_found,
          reactions_extracted,
          max_papers,
          result_data
        )
        VALUES (%s, %s, now(), %s, %s, %s, %s::jsonb)
        ON CONFLICT(material, mode) DO UPDATE SET
          created_at = excluded.created_at,
          papers_found = excluded.papers_found,
          reactions_extracted = excluded.reactions_extracted,
          max_papers = excluded.max_papers,
          result_data = excluded.result_data
        """,
        (
            material,
            mode,
            int(result_data.get("totalPapers") or 0),
            len(result_data.get("syntheses") or []) or len(result_data.get("propertyPoints") or []),
            max_papers,
            _jsonb(result_data),
        ),
    )


def list_online_history_postgres(connection: Any, limit: int = 100) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT history_id, material, mode, created_at, papers_found, reactions_extracted, max_papers, result_data
        FROM online_knowledge.history
        ORDER BY created_at DESC, history_id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [_history_row_to_dict(row) for row in rows]


def delete_online_history_postgres(connection: Any, history_id: int) -> bool:
    cursor = connection.execute(
        "DELETE FROM online_knowledge.history WHERE history_id = %s",
        (history_id,),
    )
    return cursor.rowcount > 0


def clear_online_history_postgres(connection: Any) -> None:
    connection.execute("DELETE FROM online_knowledge.history")


def create_online_job_postgres(
    connection: Any,
    *,
    job_id: str,
    material: str,
    mode: str,
    max_papers: int,
) -> None:
    connection.execute(
        """
        INSERT INTO online_knowledge.jobs (
          job_id,
          status,
          material,
          mode,
          max_papers,
          progress_stage,
          progress_message
        )
        VALUES (%s, 'pending', %s, %s, %s, 'pending', 'Waiting for the search worker to start.')
        """,
        (job_id, material, mode, max_papers),
    )


def mark_online_job_running_postgres(connection: Any, job_id: str) -> None:
    _update_online_job_postgres(
        connection,
        job_id,
        status="running",
        progress_stage="running",
        progress_message="Starting online knowledge retrieval.",
    )


def update_online_job_progress_postgres(
    connection: Any,
    job_id: str,
    *,
    stage: str,
    message: str,
    processed_papers: int = 0,
    total_papers: int = 0,
) -> None:
    connection.execute(
        """
        UPDATE online_knowledge.jobs
        SET progress_stage = %s,
            progress_message = %s,
            processed_papers = %s,
            total_papers = %s,
            updated_at = now()
        WHERE job_id = %s
        """,
        (
            stage,
            message,
            max(0, int(processed_papers)),
            max(0, int(total_papers)),
            job_id,
        ),
    )


def mark_online_job_completed_postgres(connection: Any, job_id: str, result_data: dict[str, Any]) -> None:
    connection.execute(
        """
        UPDATE online_knowledge.jobs
        SET status = 'completed',
            progress_stage = 'completed',
            progress_message = 'Online knowledge retrieval completed.',
            processed_papers = CASE
              WHEN total_papers > 0 THEN total_papers
              ELSE processed_papers
            END,
            updated_at = now(),
            error_message = NULL,
            result_data = %s::jsonb
        WHERE job_id = %s
        """,
        (_jsonb(result_data), job_id),
    )


def mark_online_job_failed_postgres(connection: Any, job_id: str, error_message: str) -> None:
    connection.execute(
        """
        UPDATE online_knowledge.jobs
        SET status = 'failed',
            progress_stage = 'failed',
            progress_message = %s,
            updated_at = now(),
            error_message = %s,
            result_data = NULL
        WHERE job_id = %s
        """,
        (error_message, error_message, job_id),
    )


def get_online_job_postgres(connection: Any, job_id: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT
          job_id,
          status,
          material,
          mode,
          max_papers,
          progress_stage,
          progress_message,
          processed_papers,
          total_papers,
          created_at,
          updated_at,
          error_message,
          result_data
        FROM online_knowledge.jobs
        WHERE job_id = %s
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    result_data = row["result_data"]
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "material": row["material"],
        "mode": row["mode"],
        "max_papers": int(row["max_papers"]),
        "progress_stage": row["progress_stage"],
        "progress_message": row["progress_message"],
        "processed_papers": int(row["processed_papers"]),
        "total_papers": int(row["total_papers"]),
        "created_at": _timestamp(row["created_at"]),
        "updated_at": _timestamp(row["updated_at"]),
        "error_message": row["error_message"],
        "result": _result_data(result_data) if result_data else None,
    }


def _update_online_job_postgres(
    connection: Any,
    job_id: str,
    *,
    status: str,
    progress_stage: str,
    progress_message: str,
) -> None:
    connection.execute(
        """
        UPDATE online_knowledge.jobs
        SET status = %s,
            progress_stage = %s,
            progress_message = %s,
            updated_at = now()
        WHERE job_id = %s
        """,
        (status, progress_stage, progress_message, job_id),
    )


def _history_row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "history_id": int(row["history_id"]),
        "material": row["material"],
        "mode": row["mode"],
        "timestamp": _timestamp(row["created_at"]),
        "papers_found": int(row["papers_found"]),
        "reactions_extracted": int(row["reactions_extracted"]),
        "max_papers": int(row["max_papers"]),
        "result_data": _result_data(row["result_data"]),
    }
