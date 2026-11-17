"""Explicit 0026 application; preview is read-only and never applies other SQL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

import psycopg
from psycopg.rows import dict_row

from app.auth.isolation_ledger import expected_isolation_ledger, validate_application_migration_ledger
from app.auth.schema import validate_isolation_schema
from app.services.retrosynthesis_schema import validate_retrosynthesis_schema
from task_usage.billing_contract import BILLING_VERSION
from task_usage.retrosynthesis_contract import RETROSYNTHESIS_VERSION, RETROSYNTHESIS_CHECKSUM


def migrate_retrosynthesis(dsn: str, *, apply: bool = False):
    source = (Path(__file__).resolve().parents[1] / "migrations/postgres" /
              (RETROSYNTHESIS_VERSION + ".sql")).read_bytes()
    if hashlib.sha256(source).hexdigest() != RETROSYNTHESIS_CHECKSUM:
        raise ValueError("Retrosynthesis migration checksum differs from this release")
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        if not apply:
            connection.execute("SET TRANSACTION READ ONLY")
        connection.execute("SET LOCAL lock_timeout='5s'")
        connection.execute("SET LOCAL statement_timeout='60s'")
        if apply:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('nexpoly-retrosynthesis-migration',0))")
            connection.execute("LOCK TABLE governance.schema_migrations IN SHARE ROW EXCLUSIVE MODE")
        observed = validate_application_migration_ledger(connection, require_usage=False, require_entitlements=False)
        if observed == expected_isolation_ledger(RETROSYNTHESIS_VERSION):
            validate_isolation_schema(connection, RETROSYNTHESIS_VERSION)
            return {"version": RETROSYNTHESIS_VERSION, "written": False, "already_applied": True}
        if observed != expected_isolation_ledger(BILLING_VERSION):
            raise ValueError("Retrosynthesis requires the exact complete 0025 prefix")
        validate_isolation_schema(connection, BILLING_VERSION)
        from app.auth.isolation_ledger import validate_service_members
        validate_service_members(connection)
        if connection.execute("SELECT to_regnamespace('retrosynthesis') AS schema").fetchone()["schema"] is not None:
            raise ValueError("Unexpected retrosynthesis schema before migration")
        if not apply:
            return {"version": RETROSYNTHESIS_VERSION, "written": False, "would_apply": True}
        connection.execute(source.decode())
        connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)",
                           (RETROSYNTHESIS_VERSION, RETROSYNTHESIS_CHECKSUM))
        validate_isolation_schema(connection, RETROSYNTHESIS_VERSION)
        validate_retrosynthesis_schema(connection)
        validate_application_migration_ledger(connection, require_usage=False, require_entitlements=False)
        return {"version": RETROSYNTHESIS_VERSION, "written": True, "already_applied": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AUTH_ADMIN_POSTGRES_DSN")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", args.dsn_env) or not os.environ.get(args.dsn_env):
        print(json.dumps({"ok": False, "code": "retrosynthesis_migration_configuration"}))
        return 2
    try:
        result = migrate_retrosynthesis(os.environ[args.dsn_env], apply=args.apply)
    except Exception:
        print(json.dumps({"ok": False, "code": "retrosynthesis_migration_failed"}))
        return 3
    print(json.dumps({"ok": True, "mode": "apply" if args.apply else "preview", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
