"""Bounded, read-only MD audit projection from the two fixed local databases.

The helper uses the already configured service credential only inside its local
backend container. Neither credentials, raw job IDs, owners nor scientific
inputs/results cross the process boundary. No business module is imported.
"""
from __future__ import annotations

import json
import re
import subprocess
import time

from .task_history_models import GpuTask, timestamp


CONTAINERS = {"development": "nexpoly_dev-backend-1", "production": "nexpoly_p9000_20261102r-backend-1"}
MAX_ROWS = 4096
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
QUERY_TIMEOUT_SECONDS = 2

# SQL is constant. In particular it cannot be supplied by the browser, a task,
# environment variables or a file. Result JSON is accessed only by fixed keys.
DATABASE_HELPER = r'''
import hashlib, json, os, sys
try:
 import psycopg
 environment = sys.argv[1]
 if environment not in {"development", "production"}: raise ValueError()
 with psycopg.connect(os.environ["APP_SERVICE_POSTGRES_DSN"], connect_timeout=1) as connection:
  connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
  connection.execute("SET LOCAL statement_timeout='750ms'")
  connection.execute("SET LOCAL lock_timeout='250ms'")
  try:
   with connection.transaction():
    rows = connection.execute("""
   SELECT job_id, created_at, started_at, finished_at, run_mode,
     gpu_device, result_data->>'gpu_uuid', result_data->>'gpu_device',
     result_data->>'execution_path',
     COALESCE(result_data->>'job_id' = job_id, false),
     COALESCE(length(worker_instance_id) BETWEEN 1 AND 128, false),
     COALESCE(length(result_data->>'gpu_lease_id') BETWEEN 1 AND 256, false),
     COALESCE(length(result_data->>'gpu_broker_instance_id') BETWEEN 1 AND 256, false),
     result_data->'gpu_fencing_token',
     CASE WHEN run_mode = 'formal' THEN
       COALESCE(result_data->>'run_mode' = 'formal' AND result_data->'physical_result' = 'true'::jsonb
         AND result_data->>'protocol' = protocol, false)
      WHEN run_mode = 'demo' THEN
       COALESCE(result_data->'demo' = 'true'::jsonb AND result_data->>'mode' = 'builtin-monomer-density-demo', false)
      ELSE false END
   FROM md.monomer_md_jobs WHERE status = 'completed'
   ORDER BY finished_at DESC NULLS LAST, job_id LIMIT 4097
    """).fetchall()
  except Exception:
   rows = None
  try:
   with connection.transaction():
    polymer_count = connection.execute("""
   SELECT count(*) FROM (SELECT 1 FROM polymer_md.jobs WHERE status='completed' LIMIT 16385) retained
    """).fetchone()[0]
  except Exception:
   polymer_count = None
 projected = []
 for row in (rows or [])[:4096]:
  job, created, started, finished, mode, device, gpu, result_device, execution, same_job, worker, lease, broker, fence, mode_verified = row
  projected.append({
   "id": hashlib.sha256((environment + "_md:" + job).encode()).hexdigest()[:20],
   "created_at": created.isoformat() if created else None,
   "started_at": started.isoformat() if started else None,
   "finished_at": finished.isoformat() if finished else None,
   "run_mode": mode if mode in {"formal", "demo"} else None,
   "gpu_uuid": gpu if isinstance(gpu, str) and len(gpu) <= 128 else None,
   "gpu_device": device if isinstance(device, str) and len(device) <= 4 else None,
   "result_gpu_device": result_device if isinstance(result_device, str) and len(result_device) <= 4 else None,
   "execution_broker": execution == "broker", "same_job": same_job,
   "worker_present": worker, "lease_present": lease, "broker_present": broker,
   "fencing_valid": type(fence) is int and fence > 0, "mode_verified": mode_verified})
 print(json.dumps({"version":1,"md_rows":projected,"md_truncated":len(rows or [])>4096,"md_available":rows is not None,
  "polymer_completed":min(polymer_count or 0,16384),"polymer_truncated":(polymer_count or 0)>16384,
  "polymer_available":polymer_count is not None}, allow_nan=False))
except Exception:
 print('{"unavailable":true}')
'''

ROW_FIELDS = {"id", "created_at", "started_at", "finished_at", "run_mode", "gpu_uuid", "gpu_device",
              "result_gpu_device", "execution_broker", "same_job", "worker_present", "lease_present",
              "broker_present", "fencing_valid", "mode_verified"}
EVIDENCE_FIELDS = {"execution_broker", "same_job", "worker_present", "lease_present", "broker_present", "fencing_valid", "mode_verified"}


def _source(source_id, *, error=None):
    return {"source_id": source_id, "status": "unavailable" if error else "ready", "matched_tasks": 0,
            "excluded_tasks": 0, "error_code": error}


def _query(environment: str, runner=subprocess.run):
    process = runner(["/usr/bin/docker", "exec", "-i", CONTAINERS[environment], "python", "-c", DATABASE_HELPER, environment],
                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                     timeout=QUERY_TIMEOUT_SECONDS, check=False)
    if process.returncode or len(process.stdout) > MAX_OUTPUT_BYTES:
        raise ValueError("database projection unavailable")
    def reject_constant(_):
        raise ValueError("non-finite database projection")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate database projection field")
            result[key] = value
        return result
    payload = json.loads(process.stdout, parse_constant=reject_constant, object_pairs_hook=unique)
    expected = {"version", "md_rows", "md_truncated", "md_available", "polymer_completed", "polymer_truncated", "polymer_available"}
    if (not isinstance(payload, dict) or set(payload) != expected or type(payload["version"]) is not int or payload["version"] != 1
            or not isinstance(payload["md_rows"], list) or len(payload["md_rows"]) > MAX_ROWS
            or type(payload["md_truncated"]) is not bool or type(payload["polymer_truncated"]) is not bool
            or type(payload["md_available"]) is not bool or type(payload["polymer_available"]) is not bool
            or type(payload["polymer_completed"]) is not int or not 0 <= payload["polymer_completed"] <= 16384):
        raise ValueError("invalid database projection")
    if ((not payload["md_available"] and (payload["md_rows"] or payload["md_truncated"]))
            or (not payload["polymer_available"] and (payload["polymer_completed"] or payload["polymer_truncated"]))):
        raise ValueError("unavailable database source cannot supply records")
    return payload


def _task(row, environment, inventory, now):
    if (not isinstance(row, dict) or set(row) != ROW_FIELDS
            or not isinstance(row["id"], str) or not re.fullmatch(r"[0-9a-f]{20}", row["id"])
            or any(type(row[field]) is not bool for field in EVIDENCE_FIELDS)):
        raise ValueError("invalid projected record")
    gpu = row["gpu_uuid"]
    # Older records and CPUs have no physical UUID; a requested CUDA ordinal is
    # never used as a substitute for an execution lease's actual UUID.
    if not isinstance(gpu, str) or gpu not in inventory:
        return None
    if not all(row[field] for field in EVIDENCE_FIELDS - {"same_job", "mode_verified"}):
        return None
    if (not row["same_job"] or not row["mode_verified"] or row["run_mode"] not in {"formal", "demo"}
            or not isinstance(row["gpu_device"], str) or not re.fullmatch(r"0|[1-9][0-9]{0,3}", row["gpu_device"])
            or int(row["gpu_device"]) > 1023 or row["result_gpu_device"] != row["gpu_device"]):
        raise ValueError("inconsistent execution evidence")
    created, started, finished = [timestamp(row[field]) for field in ("created_at", "started_at", "finished_at")]
    if (any(value.tzinfo is None or value.utcoffset() is None for value in (created, started, finished))
            or not created <= started <= finished or finished.timestamp() > now):
        raise ValueError("invalid completion timestamps")
    return GpuTask.model_validate({"id": row["id"], "gpu_uuid": gpu, "module": "monomer_md", "environment": environment,
                                  "calculation_type": "md_" + row["run_mode"], "status": "completed",
                                  "started_at": row["started_at"], "finished_at": row["finished_at"],
                                  "duration_seconds": round((finished - started).total_seconds(), 3)}).model_dump()


def collect_md_history(inventory: dict[str, int], clock=time.time, *, runner=subprocess.run):
    sources, tasks = [], []
    for environment in CONTAINERS:
        md = _source(environment + "_md")
        polymer = _source(environment + "_polymer_md")
        try:
            payload = _query(environment, runner)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, RecursionError):
            sources.extend([_source(md["source_id"], error="database_unavailable"), _source(polymer["source_id"], error="database_unavailable")])
            continue
        if payload["md_truncated"]:
            md.update(status="partial", error_code="scan_limit")
        if not payload["md_available"]:
            md.update(status="unavailable", error_code="database_unavailable")
        seen = set()
        for row in payload["md_rows"]:
            try:
                task = _task(row, environment, inventory, clock())
                if task is None:
                    md["excluded_tasks"] += 1
                elif task["id"] in seen:
                    raise ValueError("duplicate database task")
                else:
                    seen.add(task["id"]); tasks.append(task); md["matched_tasks"] += 1
            except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
                md["excluded_tasks"] += 1
                md.update(status="partial", error_code="invalid_execution_record")
        # This worker currently executes with GPU support disabled and has no
        # physical execution UUID. Count retained completions as excluded;
        # absence of supported provenance is not a database read failure.
        polymer["excluded_tasks"] = payload["polymer_completed"]
        if payload["polymer_truncated"]:
            polymer.update(status="partial", error_code="scan_limit")
        if not payload["polymer_available"]:
            polymer.update(status="unavailable", error_code="database_unavailable")
        sources.extend([md, polymer])
    return sources, tasks
