"""Personal task cohorts. Observations are statistics, never billable quantities."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.auth.context import current_owner_id
from app.postgres_database import postgres_connection
from task_usage.ledger import METRICS, STATUSES, _MODULE


def period(start: datetime | None, end: datetime | None, *, now=None):
    now = now or datetime.now(timezone.utc)
    end = end or now
    start = start or end - timedelta(days=30)
    if any(value.tzinfo is None or value.utcoffset() is None for value in (start, end)):
        raise ValueError("Date filters require an explicit timezone")
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if start >= end or end - start > timedelta(days=366):
        raise ValueError("Choose a positive interval of at most 366 days")
    return start, end


# Children inherit their root's submission cohort, even when they finish after
# midnight. Retry attempts are retained, but the task counter uses one root ID.
# Numeric attempt ordinals order retries; opaque attempts use their start/first
# occurrence. Late reports from older attempts never promote them to latest.
COHORTS = """
WITH roots AS (
  SELECT owner_user_id,module,task_id,
         min(coalesce(submitted_at,first_occurred_at)) AS cohort_at
  FROM usage.attempts
  WHERE owner_user_id=%s AND kind='task' AND parent_task_id IS NULL
  GROUP BY owner_user_id,module,task_id
), selected AS (
  SELECT * FROM roots WHERE cohort_at >= %s AND cohort_at < %s
  {module_filter}
), rows AS (
  SELECT a.*,r.module AS root_module,r.cohort_at,
    bool_or(a.kind='execution') OVER(PARTITION BY a.owner_user_id,a.root_task_id) AS has_execution
  FROM usage.attempts a JOIN selected r
    ON a.owner_user_id=r.owner_user_id AND a.root_task_id=r.task_id
  WHERE a.owner_user_id=%s
), root_attempts AS (
  SELECT *,bool_and(attempt_id ~ '^[0-9]+$') OVER(PARTITION BY root_module,task_id) AS numeric_attempts
  FROM rows WHERE kind='task' AND parent_task_id IS NULL
), latest AS (
  SELECT DISTINCT ON(root_module,task_id) * FROM root_attempts
  ORDER BY root_module,task_id,
    CASE WHEN numeric_attempts THEN attempt_id::numeric END DESC NULLS LAST,
    coalesce(started_at,first_occurred_at) DESC,first_occurred_at DESC,attempt_id DESC
)
"""


def _metric_scope(metric):
    if metric.endswith("tokens"):
        return "kind='provider'"
    if metric == "execution_ms":
        return "(kind='execution' OR (NOT has_execution AND kind='task' AND parent_task_id IS NULL))"
    return "kind='task' AND parent_task_id IS NULL"


def _empty():
    return {"tasks": 0, "attempts": 0, "child_calls": 0, "states": {},
            "metrics": {name: {"value": None, "known_count": 0, "unknown_count": 0} for name in METRICS}}


class UsageStatistics:
    def __init__(self, dsn):
        self.dsn = dsn

    def _query(self, start, end, module):
        start, end = period(start, end)
        owner = current_owner_id()
        if module is not None and not _MODULE.fullmatch(module):
            raise ValueError("Invalid module")
        args = [owner, start, end]
        if module is not None:
            args.append(module)
        args.append(owner)
        return COHORTS.format(module_filter="AND module=%s" if module else ""), args

    def summary(self, *, start, end, module=None, labels=None):
        start, end = period(start, end)
        query, params = self._query(start, end, module)
        groups, daily = {}, []
        metrics_sql = []
        for metric in METRICS:
            eligible = _metric_scope(metric)
            metrics_sql.extend([
                f"sum({metric}) FILTER(WHERE {eligible}) AS {metric}",
                f"count(*) FILTER(WHERE ({eligible}) AND {metric} IS NOT NULL) AS {metric}_known",
                f"count(*) FILTER(WHERE ({eligible}) AND {metric} IS NULL) AS {metric}_unknown",
            ])
        with postgres_connection(self.dsn, readonly_snapshot=True) as connection:
            # A consistent snapshot keeps counts and totals from different queries
            # coherent when a worker finishes while this dashboard is refreshing.
            for row in connection.execute(query + """SELECT root_module,
                count(*) FILTER(WHERE kind='task' AND parent_task_id IS NULL) AS attempts,
                count(*) FILTER(WHERE parent_task_id IS NOT NULL) AS child_calls,
                """ + ",".join(metrics_sql) + " FROM rows GROUP BY root_module ORDER BY root_module", params):
                item = _empty()
                item.update(module=row["root_module"], label=(labels or {}).get(row["root_module"], row["root_module"]),
                            attempts=row["attempts"], child_calls=row["child_calls"])
                for name in METRICS:
                    item["metrics"][name] = {"value": int(row[name]) if row[name] is not None else None,
                                             "known_count": row[name + "_known"], "unknown_count": row[name + "_unknown"]}
                groups[item["module"]] = item
            for row in connection.execute(query + "SELECT root_module,status,count(*) AS count FROM latest GROUP BY root_module,status", params):
                item = groups[row["root_module"]]
                item["states"][row["status"]] = row["count"]
                item["tasks"] += row["count"]
            dates = {}
            for row in connection.execute(query + """SELECT (cohort_at AT TIME ZONE 'UTC')::date AS date,status,count(*) AS count
                FROM latest GROUP BY date,status ORDER BY date,status""", params):
                key = row["date"].isoformat()
                item = dates.setdefault(key, {"date": key, "tasks": 0, "states": {}})
                item["tasks"] += row["count"]
                item["states"][row["status"]] = row["count"]
            daily = list(dates.values())
        totals = _empty()
        for item in groups.values():
            for key in ("tasks", "attempts", "child_calls"):
                totals[key] += item[key]
            for status, count in item["states"].items():
                totals["states"][status] = totals["states"].get(status, 0) + count
            for name, value in item["metrics"].items():
                target = totals["metrics"][name]
                for key in ("known_count", "unknown_count"):
                    target[key] += value[key]
                if value["value"] is not None:
                    target["value"] = (target["value"] or 0) + value["value"]
        return {"period": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "UTC", "basis": "task_submitted_at"},
                "totals": totals, "modules": list(groups.values()), "daily": daily}

    def records(self, *, start, end, module=None, status=None, page=1, page_size=25, include_children=False):
        if status is not None and status not in STATUSES:
            raise ValueError("Invalid status")
        if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
            raise ValueError("Invalid pagination")
        query, params = self._query(start, end, module)
        filters = ["true"]
        if not include_children:
            filters.append("kind='task' AND parent_task_id IS NULL")
        if status is not None:
            filters.append("status=%s")
            params.append(status)
        where = " AND ".join(filters)
        with postgres_connection(self.dsn, readonly_snapshot=True) as connection:
            total = connection.execute(query + "SELECT count(*) AS count FROM rows WHERE " + where, params).fetchone()["count"]
            rows = connection.execute(query + "SELECT * FROM rows WHERE " + where +
                " ORDER BY cohort_at DESC,root_module,task_id,first_occurred_at DESC,attempt_id,kind LIMIT %s OFFSET %s",
                (*params, page_size, (page - 1) * page_size))
            keys = ("module", "task_id", "attempt_id", "kind", "status", "submitted_at", "started_at", "finished_at",
                    "parent_task_id", "root_task_id", "source")
            items = [{**{key: row[key] for key in keys}, "metrics": {name: row[name] for name in METRICS}} for row in rows]
        return {"items": items, "page": page, "page_size": page_size, "total": total}
