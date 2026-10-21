"""Explicit maintenance-only initialization for the two disposable Lab databases.

No runtime request imports or calls this entry point. The owner supplies the
maintenance DSN through their trusted environment, never a command argument.
"""
from __future__ import annotations

import argparse
import json
import os

from app.auth.acceptance import LAB_DATABASES, DEADLINE, utc_now
from app.postgres_database import postgres_connection
from app.services.lab_data import LabDataService


def initialize(owner: str, *, write: bool = False) -> dict:
    from psycopg.conninfo import conninfo_to_dict
    index = {"A": 0, "B": 1}[owner]
    if utc_now() >= DEADLINE:
        raise ValueError("Acceptance maintenance authorization has expired")
    dsn = os.getenv("AUTH_ACCEPTANCE_LAB_MAINTENANCE_DSN_" + owner, "")
    try:
        if not dsn or conninfo_to_dict(dsn).get("dbname") != LAB_DATABASES[index]:
            raise ValueError
    except Exception:
        raise ValueError("Exact isolated Lab maintenance configuration is required") from None
    with postgres_connection(dsn) as connection:
        identity = connection.execute(
            "SELECT current_database() AS database, "
            "has_database_privilege(current_database(), 'CREATE') AS can_create"
        ).fetchone()
        if identity['database'] != LAB_DATABASES[index] or not identity['can_create']:
            raise ValueError("Exact isolated Lab maintenance identity is required")
        if write:
            LabDataService(connection).ensure_schema()
        return {"database": LAB_DATABASES[index], "initialized": write,
                "credentials_in_output": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, choices=("A", "B"))
    parser.add_argument("--initialize", action="store_true",
                        help="Explicitly initialize only the selected disposable Lab schema")
    args = parser.parse_args()
    try:
        result = initialize(args.owner, write=args.initialize)
    except Exception:
        parser.exit(2, "Isolated Lab maintenance preflight or initialization failed; no credentials printed.\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
