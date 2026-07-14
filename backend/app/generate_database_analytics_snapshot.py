from __future__ import annotations

import argparse
from datetime import datetime, timezone

from app.config import Settings
from app.postgres_database import postgres_connection
from app.services.analytics_snapshot_store import AnalyticsSnapshot, save_analytics_snapshot
from app.services.postgres_database_browser import get_database_analytics_postgres


def write_database_analytics_snapshot(dsn: str, *, source_sha: str | None = None) -> AnalyticsSnapshot:
    with postgres_connection(dsn) as connection:
        snapshot = get_database_analytics_postgres(connection)
        return save_analytics_snapshot(
            connection,
            snapshot,
            generated_at=datetime.now(timezone.utc),
            source_sha=source_sha,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Store the Database Analysis analytics snapshot in PostgreSQL.")
    parser.add_argument("--dsn", default=Settings().app_postgres_dsn)
    parser.add_argument("--source-sha", help="Optional 40-character source release SHA.")
    args = parser.parse_args()
    stored = write_database_analytics_snapshot(args.dsn, source_sha=args.source_sha)
    print(stored.generated_at.isoformat())


if __name__ == "__main__":
    main()
