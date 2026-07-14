from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb


DATABASE_BROWSER_SNAPSHOT_KEY = "database-browser"
REQUIRED_ANALYTICS_DATASETS = frozenset(
    {"process", "property", "structureEffect", "propertyFilter", "dft", "formulation"}
)


@dataclass(frozen=True, slots=True)
class AnalyticsSnapshot:
    generated_at: datetime
    datasets: dict[str, Any]
    source_sha: str | None = None


def save_analytics_snapshot(
    connection: Any,
    datasets: dict[str, Any],
    *,
    generated_at: datetime | None = None,
    source_sha: str | None = None,
) -> AnalyticsSnapshot:
    timestamp = generated_at or datetime.now(timezone.utc)
    row = connection.execute(
        """
        INSERT INTO governance.database_analytics_snapshots (
          snapshot_key, generated_at, source_sha, datasets, updated_at
        ) VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (snapshot_key) DO UPDATE SET
          generated_at = excluded.generated_at,
          source_sha = excluded.source_sha,
          datasets = excluded.datasets,
          updated_at = now()
        RETURNING generated_at, source_sha, datasets
        """,
        (DATABASE_BROWSER_SNAPSHOT_KEY, timestamp, source_sha, Jsonb(datasets)),
    ).fetchone()
    return _snapshot_from_row(row)


def load_analytics_snapshot(connection: Any) -> AnalyticsSnapshot | None:
    row = connection.execute(
        """
        SELECT generated_at, source_sha, datasets
        FROM governance.database_analytics_snapshots
        WHERE snapshot_key = %s
        """,
        (DATABASE_BROWSER_SNAPSHOT_KEY,),
    ).fetchone()
    return None if row is None else _snapshot_from_row(row)


def _snapshot_from_row(row: Any) -> AnalyticsSnapshot:
    datasets = row["datasets"]
    if not isinstance(datasets, dict):
        raise RuntimeError("Stored database analytics snapshot is not a JSON object")
    missing = sorted(REQUIRED_ANALYTICS_DATASETS - set(datasets))
    if missing:
        raise RuntimeError(
            "Stored database analytics snapshot is missing datasets: " + ", ".join(missing)
        )
    for name in REQUIRED_ANALYTICS_DATASETS:
        dataset = datasets[name]
        rows = dataset.get("rows") if isinstance(dataset, dict) else None
        if isinstance(rows, bool) or not isinstance(rows, int) or rows < 0:
            raise RuntimeError(
                f"Stored database analytics snapshot has an invalid row count for {name}"
            )
    return AnalyticsSnapshot(
        generated_at=row["generated_at"],
        source_sha=row["source_sha"],
        datasets=datasets,
    )
