"""Explicit, transaction-local usage projections for persistent domain tasks.

Business rows remain authoritative. Values describe observed wall time only;
no GPU/CPU duration is inferred from database reconciliation timestamps.
"""
from __future__ import annotations

from datetime import datetime
from task_usage.ledger import metering_enabled, record_event


def _time(value):
    if value is None:
        return None
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Usage timestamps must be timezone aware")
    return result


def record_persistent_task(connection, *, module, row, status=None):
    if not metering_enabled():
        return
    native_id = str(row.get("job_id", row.get("id")))
    state = status or row["status"]
    if state == "expired":
        return  # Retention must not overwrite the original execution outcome.
    state = {"pending": "queued", "submitted": "queued", "cancelling": "running",
             "completed_with_errors": "completed"}.get(state, state)
    if state == "failed" and row.get("error_code") == "job_timeout":
        state = "timed_out"
    created = _time(row.get("created_at"))
    started = _time(row.get("start_authorized_at"))
    terminal = state in {"completed", "failed", "cancelled", "timed_out", "interrupted", "rejected"}
    occurred = _time(row.get("finished_at") if terminal else None) or _time(row.get("updated_at")) or created
    if occurred is None:
        raise ValueError("Persistent usage requires a business timestamp")
    task_id = f"{module}:{native_id}"
    metrics = {}
    if terminal and created is not None:
        # Root lifecycle wall time includes queueing and, on restart, the delay
        # until recovery observes the interruption. It is not execution/CPU/GPU
        # time, and is never a sum of child timings or consumed_seconds estimates.
        metrics["observed_wall_ms"] = int((occurred - created).total_seconds() * 1000)
    event_key = f"{task_id}:1:{state}:{occurred.isoformat()}"
    if metrics:
        # An older observation of this terminal row may have all-null metrics.
        # Enrich it under a stable new key instead of conflicting with its digest.
        event_key += ":root-wall-v1"
    record_event(connection, owner_user_id=str(row["owner_user_id"]), module=module,
                 task_id=task_id, attempt_id="1", root_task_id=task_id,
                 event_key=event_key, status=state,
                 occurred_at=occurred, submitted_at=created, started_at=started,
                 finished_at=occurred if terminal else None, metrics=metrics or None)


def record_batch_attempt(connection, job, chunk, *, status, finished_at=None, elapsed=None):
    if not metering_enabled():
        return
    root_id = f"polymerization_batch:{job['id']}"
    task_id = f"{root_id}:chunk:{chunk['chunk_id']}"
    attempt = str(chunk["attempt"])
    started = _time(job.get("execution_started_at"))
    finished = _time(finished_at)
    occurred = finished or started
    if occurred is None:
        raise ValueError("Batch attempt requires an observed business timestamp")
    metrics = None if elapsed is None else {"observed_wall_ms": max(0, round(float(elapsed) * 1000))}
    record_event(connection, owner_user_id=str(job["owner_user_id"]), module="polymerization_batch",
                 task_id=task_id, attempt_id=attempt, parent_task_id=root_id, root_task_id=root_id,
                 kind="execution", event_key=f"{task_id}:{attempt}:{status}:{occurred.isoformat()}",
                 status=status, occurred_at=occurred, started_at=started, finished_at=finished,
                 metrics=metrics)
