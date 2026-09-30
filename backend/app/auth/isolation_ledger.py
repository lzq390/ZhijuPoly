"""Exact, version-bounded identity schema checks; never applies migrations."""
from __future__ import annotations

from pathlib import Path

from app.migration_policy import validate_migration_manifest_entries

PREPARE_VERSION = "0017_user_isolation_prepare"
CUTOVER_VERSION = "0018_user_isolation_cutover"
CURRENT_VERSION = "0019_service_auth_least_privilege"
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "postgres"


def expected_isolation_ledger(through: str = CURRENT_VERSION) -> list[dict[str, str]]:
    if through not in {PREPARE_VERSION, CUTOVER_VERSION, CURRENT_VERSION}:
        raise ValueError("Unsupported user isolation ledger boundary")
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    if through not in {entry.version for entry in entries}:
        raise ValueError("Requested isolation migration is absent from the release")
    return [{"version": entry.version, "checksum": entry.checksum}
            for entry in entries if entry.version <= through]


def validate_service_migration_ledger(connection, through: str = CURRENT_VERSION):
    observed = [dict(row) for row in connection.execute(
        "SELECT version,checksum FROM governance.schema_migrations ORDER BY version")]
    expected = expected_isolation_ledger(through)
    if observed != expected:
        raise ValueError("User isolation requires the exact canonical ledger through " + through)
    return observed


def database_identity(connection):
    """Bind role checks to the same database and server without credential reads."""
    row = dict(connection.execute("""SELECT current_database() AS database,
        oid AS database_oid, inet_server_addr()::text AS host,
        inet_server_port() AS port FROM pg_database WHERE datname=current_database()""").fetchone())
    if row["host"] is None:
        row["host"] = connection.info.host
        row["port"] = connection.info.port
    return row


def validate_service_members(connection):
    """Check the privilege group plus all actual member logins after restore."""
    from .service_privilege_contract import validate_service_auth_privileges
    result = [validate_service_auth_privileges(connection, role="nexpoly_service")]
    roles = connection.execute("""SELECT rolname FROM pg_roles
        WHERE rolcanlogin AND NOT rolsuper
          AND pg_has_role(oid,'nexpoly_service','MEMBER') ORDER BY rolname""")
    result.extend(validate_service_auth_privileges(connection, role=row["rolname"], require_login=True)
                  for row in roles)
    return result
