from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.auth.context import is_service_context
from app.services.private_quotas import PrivateQuotaSettings
from fastapi import HTTPException



class OnlineKnowledgeQuotaError(HTTPException):
    def __init__(self, resource: str):
        super().__init__(429, {"code": "online_retention_quota", "resource": resource,
                               "message": "本人在线检索保存数量或大小已达限额，请清理历史或联系管理员。"})


def _check_storage_quota(connection, resource: str, key, result_data=None, *, owner_user_id: str):
    """Serialize count/bytes and write in the caller's transaction, including upsert."""
    owner = owner_user_id
    quotas = PrivateQuotaSettings.from_environment()
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("online-retention:" + owner,))
    if resource == "history":
        table, exclude, params = "online_knowledge.history", "NOT(material=%s AND mode=%s)", tuple(key)
        max_rows, max_bytes = quotas.online_history_max_rows, quotas.online_history_max_bytes
    elif resource == "jobs":
        table, exclude, params = "online_knowledge.jobs", "job_id<>%s", (key,)
        max_rows, max_bytes = quotas.online_job_max_rows, quotas.online_job_max_bytes
    else:
        raise ValueError("unsupported online retention resource")
    totals = connection.execute(f"""SELECT count(*) AS rows,
        COALESCE(sum(octet_length(result_data::text)),0) AS bytes
        FROM {table} WHERE owner_user_id=%s::uuid AND {exclude}""", (owner, *params)).fetchone()
    new_bytes = 0
    if result_data is not None:
        new_bytes = connection.execute("SELECT octet_length(%s::jsonb::text) AS bytes", (_jsonb(result_data),)).fetchone()["bytes"]
    if totals["rows"] + 1 > max_rows or totals["bytes"] + new_bytes > max_bytes or new_bytes > quotas.online_result_bytes:
        raise OnlineKnowledgeQuotaError(resource)

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
    owner_user_id: str,
    material: str,
    mode: str,
    max_papers: int,
    result_data: dict[str, Any],
) -> None:
    _check_storage_quota(connection, "history", (material, mode), result_data, owner_user_id=owner_user_id)
    connection.execute(
        """
        INSERT INTO online_knowledge.history (
          owner_user_id,
          material,
          mode,
          created_at,
          papers_found,
          reactions_extracted,
          max_papers,
          result_data
        )
        VALUES (%s::uuid, %s, %s, now(), %s, %s, %s, %s::jsonb)
        ON CONFLICT(owner_user_id, material, mode) DO UPDATE SET
          created_at = excluded.created_at,
          papers_found = excluded.papers_found,
          reactions_extracted = excluded.reactions_extracted,
          max_papers = excluded.max_papers,
          result_data = excluded.result_data
        """,
        (
            owner_user_id,
            material,
            mode,
            int(result_data.get("totalPapers") or 0),
            len(result_data.get("syntheses") or []) or len(result_data.get("propertyPoints") or []),
            max_papers,
            _jsonb(result_data),
        ),
    )


def list_online_history_postgres(connection: Any, limit: int = 100, *, owner_user_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT history_id, material, mode, created_at, papers_found, reactions_extracted, max_papers, result_data
        FROM online_knowledge.history
        WHERE owner_user_id = %s::uuid
        ORDER BY created_at DESC, history_id DESC
        LIMIT %s
        """,
        (owner_user_id, limit),
    ).fetchall()
    return [_history_row_to_dict(row) for row in rows]


def delete_online_history_postgres(connection: Any, history_id: int, *, owner_user_id: str) -> bool:
    cursor = connection.execute(
        "DELETE FROM online_knowledge.history WHERE history_id = %s AND owner_user_id = %s::uuid",
        (history_id, owner_user_id),
    )
    return cursor.rowcount > 0


def clear_online_history_postgres(connection: Any, *, owner_user_id: str) -> None:
    connection.execute("DELETE FROM online_knowledge.history WHERE owner_user_id = %s::uuid", (owner_user_id,))


def create_online_job_postgres(
    connection: Any,
    *,
    owner_user_id: str,
    job_id: str,
    material: str,
    mode: str,
    max_papers: int,
) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('online-submit',0))")
    user = connection.execute("SELECT status,is_system FROM auth.users WHERE user_id=%s FOR UPDATE", (owner_user_id,)).fetchone()
    if user is None or user["status"] != "active" or user["is_system"]:
        raise HTTPException(403, "Account disabled before submission")
    _check_storage_quota(connection, "jobs", job_id, owner_user_id=owner_user_id)
    connection.execute(
        """
        INSERT INTO online_knowledge.jobs (
          job_id,
          owner_user_id,
          status,
          material,
          mode,
          max_papers,
          progress_stage,
          progress_message
        )
        VALUES (%s, %s::uuid, 'pending', %s, %s, %s, 'pending', 'Waiting for the search worker to start.')
        """,
        (job_id, owner_user_id, material, mode, max_papers),
    )


def mark_online_job_running_postgres(connection: Any, job_id: str, *, owner_user_id: str) -> None:
    _update_online_job_postgres(
        connection,
        job_id,
        owner_user_id=owner_user_id,
        status="running",
        progress_stage="running",
        progress_message="Starting online knowledge retrieval.",
    )


def update_online_job_progress_postgres(
    connection: Any,
    job_id: str,
    *,
    owner_user_id: str,
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
        WHERE job_id = %s AND owner_user_id = %s::uuid
        """,
        (
            stage,
            message,
            max(0, int(processed_papers)),
            max(0, int(total_papers)),
            job_id,
            owner_user_id,
        ),
    )


def mark_online_job_completed_postgres(connection: Any, job_id: str, result_data: dict[str, Any], *, owner_user_id: str) -> None:
    _check_storage_quota(connection, "jobs", job_id, result_data, owner_user_id=owner_user_id)
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
        WHERE job_id = %s AND owner_user_id = %s::uuid
        """,
        (_jsonb(result_data), job_id, owner_user_id),
    )


def mark_online_job_failed_postgres(connection: Any, job_id: str, error_message: str, *, owner_user_id: str) -> None:
    connection.execute(
        """
        UPDATE online_knowledge.jobs
        SET status = 'failed',
            progress_stage = 'failed',
            progress_message = %s,
            updated_at = now(),
            error_message = %s,
            result_data = NULL
        WHERE job_id = %s AND owner_user_id = %s::uuid
        """,
        (error_message, error_message, job_id, owner_user_id),
    )


def get_online_job_postgres(connection: Any, job_id: str, *, owner_user_id: str) -> dict[str, Any] | None:
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
        WHERE job_id = %s AND owner_user_id = %s::uuid
        """,
        (job_id, owner_user_id),
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
    owner_user_id: str,
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
        WHERE job_id = %s AND owner_user_id = %s::uuid
        """,
        (status, progress_stage, progress_message, job_id, owner_user_id),
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


def fail_interrupted_online_jobs_postgres(connection: Any) -> int:
    """Call once at single-backend startup, before accepting submissions."""
    if not is_service_context():
        raise RuntimeError("Global online recovery requires an explicit service context")
    cursor = connection.execute("""UPDATE online_knowledge.jobs SET status='failed',
        progress_stage='failed', progress_message='Search interrupted by backend restart.',
        error_message='worker_restarted', updated_at=now()
        WHERE status IN ('pending', 'running')""")
    return cursor.rowcount
