from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from pprint import pformat
from typing import Any

from app.config import PROJECT_ROOT, Settings
from app.postgres_database import postgres_connection
from app.services.postgres_database_browser import get_database_analytics_postgres

DEFAULT_SNAPSHOT_PATH = PROJECT_ROOT / "backend" / "app" / "services" / "database_analytics_snapshot.py"


def build_snapshot_module(snapshot: dict[str, Any], generated_at: str) -> str:
    return """from __future__ import annotations

from copy import deepcopy
from typing import Any

STATIC_DATABASE_ANALYTICS_GENERATED_AT = {generated_at!r}
STATIC_DATABASE_ANALYTICS: dict[str, Any] = {snapshot}


def get_database_analytics_snapshot() -> dict[str, Any]:
    return deepcopy(STATIC_DATABASE_ANALYTICS)
""".format(
        generated_at=generated_at,
        snapshot=pformat(snapshot, width=120, sort_dicts=True),
    )


def write_database_analytics_snapshot(dsn: str, output_path: Path = DEFAULT_SNAPSHOT_PATH) -> Path:
    with postgres_connection(dsn) as connection:
        snapshot = get_database_analytics_postgres(connection)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        build_snapshot_module(snapshot, datetime.now(timezone.utc).isoformat()),
        encoding="utf-8",
    )
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the static Database Analysis analytics snapshot.")
    parser.add_argument("--dsn", default=Settings().app_postgres_dsn)
    parser.add_argument("--output", type=Path, default=DEFAULT_SNAPSHOT_PATH)
    args = parser.parse_args()
    output_path = write_database_analytics_snapshot(args.dsn, args.output)
    print(output_path)


if __name__ == "__main__":
    main()
