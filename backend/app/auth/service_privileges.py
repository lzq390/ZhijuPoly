"""Explicit, atomic 0018 -> 0019 service privilege maintenance.

Ordinary bootstrap never invokes this module. Only catalog metadata is read;
no account, session, credential, or scientific workload data is projected.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.migration_policy import validate_migration_manifest_entries
from app.postgres_migrations import MIGRATIONS_DIR
from .isolation_ledger import validate_service_migration_ledger
from .service_privilege_contract import VERSION, validate_service_auth_privileges

PREDECESSOR = "0018_user_isolation_cutover"


def apply_service_auth_least_privilege(
    dsn: str, *, service_roles: list[str], expected_database: str,
    expected_system_identifier: str | None = None,
) -> dict:
    """Contract one exact ledger in one transaction, rechecking ACLs on retry.

    The caller supplies actual LOGIN roles, not DSNs or the privilege group.
    Any unexpected grant causes rollback; this is not a privilege repair tool.
    """
    if (not isinstance(service_roles, (list, tuple)) or not service_roles
            or any(not isinstance(role, str) or not role.strip() for role in service_roles)
            or len(set(service_roles)) != len(service_roles)
            or "nexpoly_service" in service_roles):
        raise ValueError("Specify unique actual service LOGIN roles")
    if not isinstance(expected_database, str) or not expected_database:
        raise ValueError("An explicit expected database name is required")
    if expected_system_identifier is not None and (
        not isinstance(expected_system_identifier, str)
        or not expected_system_identifier.isdecimal()
    ):
        raise ValueError("Expected system identifier must be a decimal string")
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    target = next(entry for entry in entries if entry.version == VERSION)
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        connection.execute("SET LOCAL lock_timeout='10s'")
        connection.execute("SET LOCAL statement_timeout='60s'")
        connection.execute("SET LOCAL search_path=pg_catalog")
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('nexpoly-identity-cutover',0))"
        )
        identity = dict(connection.execute("""SELECT current_database() AS name,
            (SELECT oid FROM pg_database WHERE datname=current_database()) AS oid,
            system_identifier::text AS system_identifier FROM pg_control_system()""").fetchone())
        if identity["name"] != expected_database or (
            expected_system_identifier is not None
            and identity["system_identifier"] != expected_system_identifier
        ):
            raise ValueError("Maintenance target does not match the expected database identity")
        # Exact ledger validation rejects unknown, duplicate and changed records.
        installed = connection.execute(
            "SELECT 1 FROM governance.schema_migrations WHERE version=%s", (VERSION,)
        ).fetchone() is not None
        validate_service_migration_ledger(connection, through=VERSION if installed else PREDECESSOR)
        before = {
            "nexpoly_service": validate_service_auth_privileges(
                connection, "nexpoly_service", legacy=not installed,
            ),
            **{role: validate_service_auth_privileges(
                connection, role, require_login=True, legacy=not installed,
            ) for role in service_roles},
        }
        if not installed:
            connection.execute((MIGRATIONS_DIR / (VERSION + ".sql")).read_text())
        after = {
            "nexpoly_service": validate_service_auth_privileges(connection, "nexpoly_service"),
            **{role: validate_service_auth_privileges(connection, role, require_login=True)
               for role in service_roles},
        }
        if not installed:
            connection.execute(
                "INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)",
                (VERSION, target.checksum),
            )
        ledger = validate_service_migration_ledger(connection, through=VERSION)
        return {
            "schema_version": 1, "version": VERSION, "checksum": target.checksum,
            "database": identity, "already_applied": installed,
            "service_roles": list(service_roles), "before": before, "after": after,
            "migration_ledger": ledger, "credential_values_read": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", required=True, help="Environment variable containing the maintenance DSN")
    parser.add_argument("--expected-database", required=True)
    parser.add_argument("--expected-system-identifier", required=True)
    parser.add_argument("--service-role", action="append", required=True, dest="service_roles")
    parser.add_argument("--audit-output", type=Path, required=True)
    args = parser.parse_args()
    # Reserve an exclusive private record before any database connection/mutation.
    descriptor = os.open(args.audit_output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as audit:
        def record(value):
            audit.seek(0)
            audit.truncate()
            audit.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
            audit.flush()
            os.fsync(audit.fileno())

        record({"version": VERSION, "state": "started", "commit_confirmed": False})
        try:
            result = apply_service_auth_least_privilege(
                os.environ[args.dsn_env], service_roles=args.service_roles,
                expected_database=args.expected_database,
                expected_system_identifier=args.expected_system_identifier,
            )
        except Exception as exc:
            # Driver errors may contain DSNs or connection credentials. Never
            # serialize their text or emit a chained traceback in this CLI.
            try:
                record({"version": VERSION, "state": "failed", "commit_confirmed": False,
                        "error_type": type(exc).__name__, "action": "inspect target ledger and ACLs before retry"})
            except OSError:
                raise SystemExit(
                    "Service privilege maintenance failed and its audit could not be written; "
                    "verify the target ledger and ACLs before retry"
                ) from None
            raise SystemExit("Service privilege maintenance failed; inspect the private audit record and target state") from None
        try:
            record({**result, "state": "complete", "commit_confirmed": True})
        except OSError:
            raise SystemExit(
                "Service privilege transaction returned success, but the audit write failed; "
                "verify the target ledger and ACLs before retry. Do not reverse the migration."
            ) from None
    print(f"Completed {VERSION}; audit: {args.audit_output}")


if __name__ == "__main__":
    main()
