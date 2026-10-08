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



USAGE_VERSION = "0020_usage_metering"


from task_usage.entitlements import MIGRATION_VERSION as ENTITLEMENT_VERSION


def validate_application_migration_ledger(connection, *, require_usage: bool | None = None,
                                          require_entitlements: bool | None = None):
    """Accept only exact release-known 0019/0020/0021 ledgers; never unknown suffixes."""
    from task_usage.ledger import metering_enabled
    from task_usage.entitlements import enabled
    require_usage = metering_enabled() if require_usage is None else require_usage
    require_entitlements = enabled() if require_entitlements is None else require_entitlements
    observed = [dict(row) for row in connection.execute(
        "SELECT version,checksum FROM governance.schema_migrations ORDER BY version")]
    legacy = expected_isolation_ledger(CURRENT_VERSION)
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    usage = next((entry for entry in entries if entry.version == USAGE_VERSION), None)
    ent = next((entry for entry in entries if entry.version == ENTITLEMENT_VERSION), None)
    if usage is None or usage.kind != "expand" or usage.epoch != 5:
        raise ValueError("Usage expansion is absent from the release manifest")
    if ent is None or ent.kind != "expand" or ent.epoch != 6:
        raise ValueError("Entitlement expansion is absent from the release manifest")
    expanded = [*legacy, {"version": usage.version, "checksum": usage.checksum}]
    entitled = [*expanded, {"version": ent.version, "checksum": ent.checksum}]
    accepted = [entitled] if require_entitlements else [expanded, entitled] if require_usage else [legacy, expanded, entitled]
    if observed not in accepted:
        raise ValueError("Application requires an exact canonical 0019, 0020 or 0021 ledger")
    if require_usage or require_entitlements:
        from app.services.usage_schema import validate_usage_schema
        validate_usage_schema(connection)
    if require_entitlements:
        from app.services.entitlement_schema import validate_entitlement_schema
        validate_entitlement_schema(connection)
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
