"""Owner-isolated, append-only usage observations, never a billing authority.

Metrics are nullable cumulative observations per attempt, not deltas. A missing
measurement is unknown; it is never inferred as zero or as physical GPU time.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Any
from uuid import UUID


METRICS = (
    "queue_ms", "execution_ms", "observed_wall_ms", "provider_input_tokens",
    "provider_output_tokens", "provider_total_tokens", "cached_input_tokens",
    "reasoning_tokens", "artifact_bytes",
)
STATUSES = frozenset({"queued", "running", "completed", "failed", "cancelled", "interrupted", "rejected", "timed_out"})
KINDS = frozenset({"task", "execution", "provider"})
SOURCES = frozenset({"runtime", "backfill", "reconciliation"})
TERMINAL_STATUSES = STATUSES - {"queued", "running"}
_MODULE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_BIGINT = 2**63 - 1


def metering_enabled() -> bool:
    """An explicit opt-in; absence never changes existing business behavior."""
    return os.environ.get("USAGE_METERING_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


class UsageEventConflict(ValueError):
    """An existing idempotency key identifies different immutable observations."""


def _text(value: object, name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} must be a bounded, nonempty identifier")
    return value


def _time(value: datetime | None, name: str, *, required: bool = False) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _normalize(*, owner_user_id: str, module: str, task_id: str, attempt_id: str,
               event_key: str, status: str, occurred_at: datetime,
               submitted_at: datetime | None, started_at: datetime | None,
               finished_at: datetime | None, parent_task_id: str | None,
               root_task_id: str | None, source: str, kind: str,
               metrics: dict[str, int | None] | None) -> dict[str, Any]:
    owner = str(UUID(str(owner_user_id)))
    if not isinstance(module, str) or not _MODULE.fullmatch(module):
        raise ValueError("module must be a lowercase catalog identifier")
    if status not in STATUSES or source not in SOURCES or kind not in KINDS:
        raise ValueError("Unsupported usage status, source or kind")
    task_id = _text(task_id, "task_id")
    if parent_task_id is not None:
        parent_task_id = _text(parent_task_id, "parent_task_id")
        if parent_task_id == task_id or root_task_id is None:
            raise ValueError("A child requires a distinct parent and explicit root_task_id")
    root_task_id = _text(root_task_id or task_id, "root_task_id")
    if parent_task_id is None and root_task_id != task_id:
        raise ValueError("A root observation must identify its own task as root")
    times = {key: _time(value, key, required=key == "occurred_at") for key, value in (
        ("occurred_at", occurred_at), ("submitted_at", submitted_at),
        ("started_at", started_at), ("finished_at", finished_at),
    )}
    sequence = [times[key] for key in ("submitted_at", "started_at", "finished_at") if times[key] is not None]
    if sequence != sorted(sequence):
        raise ValueError("Usage timestamps are out of order")
    if finished_at is not None and status not in TERMINAL_STATUSES:
        raise ValueError("Only a terminal observation may supply finished_at")
    if metrics is not None and (not isinstance(metrics, dict) or set(metrics) - set(METRICS)):
        raise ValueError("Unsupported usage metric")
    values = {key: (metrics or {}).get(key) for key in METRICS}
    if any(value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                 or not 0 <= value <= _MAX_BIGINT) for value in values.values()):
        raise ValueError("Usage metrics must be nullable nonnegative bigint integers")
    return dict(owner_user_id=owner, module=module, task_id=task_id,
                attempt_id=_text(attempt_id, "attempt_id", 128),
                event_key=_text(event_key, "event_key"), status=status, kind=kind,
                root_task_id=root_task_id, parent_task_id=parent_task_id, source=source,
                **times, **values)


def record_event(connection, *, owner_user_id: str, module: str, task_id: str,
                 attempt_id: str = "1", event_key: str, status: str,
                 occurred_at: datetime, submitted_at: datetime | None = None,
                 started_at: datetime | None = None, finished_at: datetime | None = None,
                 parent_task_id: str | None = None, root_task_id: str | None = None,
                 source: str = "runtime", kind: str = "task",
                 metrics: dict[str, int | None] | None = None) -> dict[str, Any]:
    """Append and project in the caller's transaction; never connects or commits.

    Identity is (owner, module, task, attempt, kind). Re-delivery of exactly the
    same normalized event is a no-op; conflicting event keys raise explicitly.
    Callers must use stable source timestamps/keys when retrying delivery.
    """
    if not metering_enabled():
        return {"inserted": False, "disabled": True, "event_key": event_key, "attempt": None}
    from psycopg.rows import dict_row
    event = _normalize(owner_user_id=owner_user_id, module=module, task_id=task_id,
        attempt_id=attempt_id, event_key=event_key, status=status, occurred_at=occurred_at,
        submitted_at=submitted_at, started_at=started_at, finished_at=finished_at,
        parent_task_id=parent_task_id, root_task_id=root_task_id, source=source,
        kind=kind, metrics=metrics)
    canonical = {key: value.isoformat() if isinstance(value, datetime) else value
                 for key, value in event.items()}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    fields = tuple(event)
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "INSERT INTO usage.events (" + ",".join(fields) + ",payload_sha256) VALUES ("
            + ",".join(["%s"] * (len(fields) + 1))
            + ") ON CONFLICT (owner_user_id,event_key) DO NOTHING RETURNING event_key",
            tuple(event.values()) + (digest,),
        )
        inserted = cursor.fetchone() is not None
        if not inserted:
            cursor.execute("SELECT payload_sha256 FROM usage.events WHERE owner_user_id=%s AND event_key=%s",
                           (event["owner_user_id"], event["event_key"]))
            prior = cursor.fetchone()
            if prior is None or prior["payload_sha256"] != digest:
                raise UsageEventConflict("Usage event key was reused with a different payload")
        cursor.execute("""SELECT * FROM usage.attempts WHERE owner_user_id=%s AND module=%s
            AND task_id=%s AND attempt_id=%s AND kind=%s""",
            (event["owner_user_id"], module, task_id, attempt_id, kind))
        projection = cursor.fetchone()
    return {"inserted": inserted, "event_key": event["event_key"], "attempt": dict(projection)}
