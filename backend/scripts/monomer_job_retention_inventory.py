#!/usr/bin/env python3
"""Read-only MD/DFT retention inventory for rollout planning."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.postgres_database import postgres_connection  # noqa: E402


def _estimated_json_bytes(value: Any) -> int:
    if isinstance(value, dict):
        direct = value.get("size_bytes")
        own = (
            int(direct)
            if isinstance(direct, int)
            and not isinstance(direct, bool)
            and direct >= 0
            else 0
        )
        return own + sum(
            _estimated_json_bytes(item)
            for key, item in value.items()
            if key != "size_bytes"
        )
    if isinstance(value, list):
        return sum(_estimated_json_bytes(item) for item in value)
    return 0


def inventory(dsn: str, days: int) -> dict[str, Any]:
    with postgres_connection(dsn) as connection:
        md_summary = connection.execute(
            """
            SELECT count(*) AS records,
                   min(COALESCE(finished_at, updated_at)) AS oldest_terminal_at,
                   count(*) FILTER (WHERE run_mode = 'demo') AS demo_records,
                   count(*) FILTER (WHERE run_mode = 'formal') AS formal_records,
                   count(*) FILTER (WHERE artifact_root IS NOT NULL) AS artifact_roots
            FROM md.monomer_md_jobs
            WHERE status IN ('completed', 'failed', 'cancelled')
              AND created_at <= now() - (%s * interval '1 day')
              AND COALESCE(finished_at, updated_at) <=
                  now() - (%s * interval '1 day')
            """,
            (days, days),
        ).fetchone()
        md_artifacts = connection.execute(
            """
            SELECT artifacts, artifact_manifest
            FROM md.monomer_md_jobs
            WHERE status IN ('completed', 'failed', 'cancelled')
              AND created_at <= now() - (%s * interval '1 day')
              AND COALESCE(finished_at, updated_at) <=
                  now() - (%s * interval '1 day')
            """,
            (days, days),
        ).fetchall()
        dft_summary = connection.execute(
            """
            SELECT count(DISTINCT j.job_id) AS records,
                   min(COALESCE(j.finished_at, j.updated_at)) AS oldest_terminal_at,
                   COALESCE(sum(a.size_bytes) FILTER (WHERE a.available), 0)
                     AS available_artifact_bytes
            FROM monomer_dft.jobs j
            LEFT JOIN monomer_dft.artifacts a ON a.job_id = j.job_id
            WHERE j.status IN ('completed', 'failed', 'cancelled')
              AND j.created_at <= now() - (%s * interval '1 day')
              AND COALESCE(j.finished_at, j.updated_at) <=
                  now() - (%s * interval '1 day')
            """,
            (days, days),
        ).fetchone()
    md_estimated_bytes = sum(
        _estimated_json_bytes(row["artifacts"])
        or _estimated_json_bytes(row["artifact_manifest"])
        for row in md_artifacts
    )
    return {
        "retention_days": days,
        "read_only": True,
        "monomer_md": {
            "records": int(md_summary["records"]),
            "demo_records": int(md_summary["demo_records"]),
            "formal_records": int(md_summary["formal_records"]),
            "oldest_terminal_at": (
                md_summary["oldest_terminal_at"].isoformat()
                if md_summary["oldest_terminal_at"]
                else None
            ),
            "artifact_roots": int(md_summary["artifact_roots"]),
            "estimated_artifact_bytes_from_metadata": md_estimated_bytes,
        },
        "monomer_dft": {
            "records": int(dft_summary["records"]),
            "oldest_terminal_at": (
                dft_summary["oldest_terminal_at"].isoformat()
                if dft_summary["oldest_terminal_at"]
                else None
            ),
            "available_artifact_bytes": int(
                dft_summary["available_artifact_bytes"]
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default=os.getenv("APP_POSTGRES_DSN", ""),
        help="PostgreSQL DSN (defaults to APP_POSTGRES_DSN)",
    )
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if not args.dsn.strip():
        parser.error("--dsn or APP_POSTGRES_DSN is required")
    if not 1 <= args.days <= 3650:
        parser.error("--days must be between 1 and 3650")
    print(json.dumps(inventory(args.dsn, args.days), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
