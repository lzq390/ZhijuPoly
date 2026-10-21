"""Owner-scoped usage queries; writes share the worker-safe task_usage protocol."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.auth.context import current_owner_id
from app.postgres_database import postgres_connection
from task_usage.ledger import (METRICS, STATUSES, KINDS, SOURCES, TERMINAL_STATUSES,
                               UsageEventConflict, metering_enabled, record_event, _MODULE, _time)


class UsageRepository:
    def __init__(self, dsn: str):
        self.dsn = dsn

    def record_event(self, connection=None, **event) -> dict[str, Any]:
        if not metering_enabled():
            return {"inserted": False, "disabled": True, "event_key": event.get("event_key"), "attempt": None}
        if connection is not None:
            return record_event(connection, **event)
        with postgres_connection(self.dsn) as active:
            return record_event(active, **event)

    @staticmethod
    def _filters(*, module: str | None, start_at: datetime | None, end_at: datetime | None):
        owner = current_owner_id()
        clauses, params = ["owner_user_id=%s"], [owner]
        if module is not None:
            if not _MODULE.fullmatch(module):
                raise ValueError("Invalid module")
            clauses.append("module=%s")
            params.append(module)
        start_at, end_at = _time(start_at, "start_at"), _time(end_at, "end_at")
        if start_at is not None and end_at is not None and start_at >= end_at:
            raise ValueError("start_at must precede end_at")
        for value, clause in ((start_at, "first_occurred_at >= %s"), (end_at, "first_occurred_at < %s")):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        return " AND ".join(clauses), params

    def list_attempts(self, *, module: str | None = None, start_at: datetime | None = None,
                      end_at: datetime | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """The owner comes exclusively from server identity, including service contexts."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100000:
            raise ValueError("offset must be between 0 and 100000")
        where, params = self._filters(module=module, start_at=start_at, end_at=end_at)
        with postgres_connection(self.dsn) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM usage.attempts WHERE " + where
                + " ORDER BY first_occurred_at DESC,module,task_id,attempt_id,kind LIMIT %s OFFSET %s",
                (*params, limit, offset))]

    def summary(self, *, module: str | None = None, start_at: datetime | None = None,
                end_at: datetime | None = None) -> dict[str, Any]:
        """Task counts deduplicate retries; metric totals stay separated by kind.

        Window membership uses the attempt's first observed occurrence, [start,
        end). Do not add task wall time to nested execution/provider wall time.
        """
        where, params = self._filters(module=module, start_at=start_at, end_at=end_at)
        metric_sql = ",".join(f"sum({metric}) AS {metric},count({metric}) AS {metric}_known_count"
                              for metric in METRICS)
        with postgres_connection(self.dsn) as connection:
            task_count = connection.execute("SELECT count(DISTINCT (module,task_id)) AS count FROM usage.attempts WHERE "
                + where + " AND kind='task' AND parent_task_id IS NULL", params).fetchone()["count"]
            rows = connection.execute("SELECT module,kind,count(*) AS attempt_count," + metric_sql
                + " FROM usage.attempts WHERE " + where + " GROUP BY module,kind ORDER BY module,kind", params)
            groups = []
            for row in rows:
                groups.append({"module": row["module"], "kind": row["kind"], "attempt_count": row["attempt_count"],
                    "metrics": {metric: {"total": int(row[metric]) if row[metric] is not None else None,
                                           "known_count": row[metric + "_known_count"]} for metric in METRICS}})
        return {"task_count": task_count, "groups": groups, "window_basis": "first_occurred_at"}
