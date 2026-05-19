from __future__ import annotations

import json
import sqlite3
from typing import Any


ONLINE_KNOWLEDGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS online_knowledge_history (
  history_id INTEGER PRIMARY KEY AUTOINCREMENT,
  material TEXT NOT NULL,
  mode TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  papers_found INTEGER NOT NULL DEFAULT 0,
  reactions_extracted INTEGER NOT NULL DEFAULT 0,
  max_papers INTEGER NOT NULL DEFAULT 0,
  result_json TEXT NOT NULL,
  UNIQUE(material, mode)
);

CREATE INDEX IF NOT EXISTS idx_online_knowledge_created_at
ON online_knowledge_history(created_at);

CREATE TABLE IF NOT EXISTS online_knowledge_jobs (
  job_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  material TEXT NOT NULL,
  mode TEXT NOT NULL,
  max_papers INTEGER NOT NULL,
  progress_stage TEXT NOT NULL DEFAULT 'pending',
  progress_message TEXT NOT NULL DEFAULT 'Waiting for the search worker to start.',
  processed_papers INTEGER NOT NULL DEFAULT 0,
  total_papers INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  error_message TEXT,
  result_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_online_knowledge_jobs_created_at
ON online_knowledge_jobs(created_at);
"""


def ensure_online_knowledge_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(ONLINE_KNOWLEDGE_SCHEMA_SQL)
    _ensure_online_job_progress_columns(connection)


def save_online_history(
    connection: sqlite3.Connection,
    *,
    material: str,
    mode: str,
    max_papers: int,
    result_data: dict[str, Any],
) -> None:
    ensure_online_knowledge_schema(connection)
    connection.execute(
        """
        INSERT INTO online_knowledge_history (
          material,
          mode,
          created_at,
          papers_found,
          reactions_extracted,
          max_papers,
          result_json
        )
        VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
        ON CONFLICT(material, mode) DO UPDATE SET
          created_at = excluded.created_at,
          papers_found = excluded.papers_found,
          reactions_extracted = excluded.reactions_extracted,
          max_papers = excluded.max_papers,
          result_json = excluded.result_json
        """,
        (
            material,
            mode,
            int(result_data.get("totalPapers") or 0),
            len(result_data.get("syntheses") or []) or len(result_data.get("propertyPoints") or []),
            max_papers,
            json.dumps(result_data, ensure_ascii=False),
        ),
    )


def list_online_history(connection: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    ensure_online_knowledge_schema(connection)
    rows = connection.execute(
        """
        SELECT history_id, material, mode, created_at, papers_found, reactions_extracted, max_papers, result_json
        FROM online_knowledge_history
        ORDER BY datetime(created_at) DESC, history_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_history_row_to_dict(row) for row in rows]


def delete_online_history(connection: sqlite3.Connection, history_id: int) -> bool:
    ensure_online_knowledge_schema(connection)
    cursor = connection.execute(
        "DELETE FROM online_knowledge_history WHERE history_id = ?",
        (history_id,),
    )
    return cursor.rowcount > 0


def clear_online_history(connection: sqlite3.Connection) -> None:
    ensure_online_knowledge_schema(connection)
    connection.execute("DELETE FROM online_knowledge_history")


def create_online_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    material: str,
    mode: str,
    max_papers: int,
) -> None:
    ensure_online_knowledge_schema(connection)
    connection.execute(
        """
        INSERT INTO online_knowledge_jobs (
          job_id,
          status,
          material,
          mode,
          max_papers,
          progress_stage,
          progress_message
        )
        VALUES (?, 'pending', ?, ?, ?, 'pending', 'Waiting for the search worker to start.')
        """,
        (job_id, material, mode, max_papers),
    )


def mark_online_job_running(connection: sqlite3.Connection, job_id: str) -> None:
    _update_online_job(
        connection,
        job_id,
        status="running",
        progress_stage="running",
        progress_message="Starting online knowledge retrieval.",
    )


def update_online_job_progress(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    stage: str,
    message: str,
    processed_papers: int = 0,
    total_papers: int = 0,
) -> None:
    ensure_online_knowledge_schema(connection)
    connection.execute(
        """
        UPDATE online_knowledge_jobs
        SET progress_stage = ?,
            progress_message = ?,
            processed_papers = ?,
            total_papers = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE job_id = ?
        """,
        (
            stage,
            message,
            max(0, int(processed_papers)),
            max(0, int(total_papers)),
            job_id,
        ),
    )


def mark_online_job_completed(connection: sqlite3.Connection, job_id: str, result_data: dict[str, Any]) -> None:
    ensure_online_knowledge_schema(connection)
    connection.execute(
        """
        UPDATE online_knowledge_jobs
        SET status = 'completed',
            progress_stage = 'completed',
            progress_message = 'Online knowledge retrieval completed.',
            processed_papers = CASE
              WHEN total_papers > 0 THEN total_papers
              ELSE processed_papers
            END,
            updated_at = CURRENT_TIMESTAMP,
            error_message = NULL,
            result_json = ?
        WHERE job_id = ?
        """,
        (json.dumps(result_data, ensure_ascii=False), job_id),
    )


def mark_online_job_failed(connection: sqlite3.Connection, job_id: str, error_message: str) -> None:
    ensure_online_knowledge_schema(connection)
    connection.execute(
        """
        UPDATE online_knowledge_jobs
        SET status = 'failed',
            progress_stage = 'failed',
            progress_message = ?,
            updated_at = CURRENT_TIMESTAMP,
            error_message = ?,
            result_json = NULL
        WHERE job_id = ?
        """,
        (error_message, error_message, job_id),
    )


def get_online_job(connection: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    ensure_online_knowledge_schema(connection)
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
          result_json
        FROM online_knowledge_jobs
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    result_json = row["result_json"]
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
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "error_message": row["error_message"],
        "result": json.loads(result_json) if result_json else None,
    }


def _update_online_job(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    status: str,
    progress_stage: str,
    progress_message: str,
) -> None:
    ensure_online_knowledge_schema(connection)
    connection.execute(
        """
        UPDATE online_knowledge_jobs
        SET status = ?,
            progress_stage = ?,
            progress_message = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE job_id = ?
        """,
        (status, progress_stage, progress_message, job_id),
    )


def _history_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result_data = json.loads(row["result_json"])
    return {
        "history_id": int(row["history_id"]),
        "material": row["material"],
        "mode": row["mode"],
        "timestamp": row["created_at"],
        "papers_found": int(row["papers_found"]),
        "reactions_extracted": int(row["reactions_extracted"]),
        "max_papers": int(row["max_papers"]),
        "result_data": result_data,
    }


def _ensure_online_job_progress_columns(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(online_knowledge_jobs)").fetchall()
    }
    column_sql = {
        "progress_stage": "ALTER TABLE online_knowledge_jobs ADD COLUMN progress_stage TEXT NOT NULL DEFAULT 'pending'",
        "progress_message": (
            "ALTER TABLE online_knowledge_jobs "
            "ADD COLUMN progress_message TEXT NOT NULL DEFAULT 'Waiting for the search worker to start.'"
        ),
        "processed_papers": "ALTER TABLE online_knowledge_jobs ADD COLUMN processed_papers INTEGER NOT NULL DEFAULT 0",
        "total_papers": "ALTER TABLE online_knowledge_jobs ADD COLUMN total_papers INTEGER NOT NULL DEFAULT 0",
    }
    for column, statement in column_sql.items():
        if column not in existing_columns:
            connection.execute(statement)
