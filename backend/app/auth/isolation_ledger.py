"""Exact, version-bounded identity schema checks; never applies migrations."""
from __future__ import annotations

from pathlib import Path

from app.migration_policy import validate_migration_manifest_entries
from app.literature_contract import (compatible_identity_ledger, LITERATURE_MIGRATION as LITERATURE_VERSION,
                                     LITERATURE_MIGRATION_SHA256)

PREPARE_VERSION = "0017_user_isolation_prepare"
CUTOVER_VERSION = "0018_user_isolation_cutover"
CURRENT_VERSION = "0019_service_auth_least_privilege"
LIBRARY_VERSION = "0020_structure_library"
POLYMER_VERSION = "0023_polymer_md_jobs"
POLYMER_CHECKSUM = "e73e00e16b8fc3c72e3716dae523d02d264a3e2e6989671f6019aca3440a1379"
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "postgres"


def expected_isolation_ledger(through: str = CURRENT_VERSION) -> list[dict[str, str]]:
    if through not in {PREPARE_VERSION, CUTOVER_VERSION, CURRENT_VERSION, LIBRARY_VERSION, USAGE_VERSION, ENTITLEMENT_VERSION, POLYMER_VERSION, LITERATURE_VERSION}:
        raise ValueError("Unsupported user isolation ledger boundary")
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    if through not in {entry.version for entry in entries}:
        raise ValueError("Requested isolation migration is absent from the release")
    if through == POLYMER_VERSION:
        target = next(entry for entry in entries if entry.version == POLYMER_VERSION)
        if target.kind != "expand" or target.epoch != 8 or target.checksum != POLYMER_CHECKSUM:
            raise ValueError("Optional polymer MD migration differs from the approved release")
    if through == LITERATURE_VERSION:
        target = next(entry for entry in entries if entry.version == LITERATURE_VERSION)
        if target.kind != "expand" or target.epoch != 9 or target.checksum != LITERATURE_MIGRATION_SHA256:
            raise ValueError("Optional literature migration differs from the approved release")
    return [{"version": entry.version, "checksum": entry.checksum}
            for entry in entries if entry.version <= through]


def validate_service_migration_ledger(connection, through: str = CURRENT_VERSION):
    observed = [dict(row) for row in connection.execute(
        "SELECT version,checksum FROM governance.schema_migrations ORDER BY version")]
    expected = expected_isolation_ledger(through)
    if not compatible_identity_ledger(observed, expected):
        raise ValueError("User isolation requires the exact canonical ledger through " + through)
    return observed



USAGE_VERSION = "0021_usage_metering"


from task_usage.entitlements import MIGRATION_VERSION as ENTITLEMENT_VERSION


def validate_application_migration_ledger(connection, *, require_usage: bool | None = None,
                                          require_entitlements: bool | None = None):
    """Accept exact release-known prefixes, including every intervening expansion."""
    from task_usage.ledger import metering_enabled
    from task_usage.entitlements import enabled
    require_usage = metering_enabled() if require_usage is None else require_usage
    require_entitlements = enabled() if require_entitlements is None else require_entitlements
    observed = [dict(row) for row in connection.execute(
        "SELECT version,checksum FROM governance.schema_migrations ORDER BY version")]
    entries = validate_migration_manifest_entries(MIGRATIONS_DIR)
    by_version = {entry.version: entry for entry in entries}
    for version, epoch in ((LIBRARY_VERSION, 5), (USAGE_VERSION, 6), (ENTITLEMENT_VERSION, 7), (POLYMER_VERSION, 8), (LITERATURE_VERSION, 9)):
        entry = by_version.get(version)
        if entry is None or entry.kind != "expand" or entry.epoch != epoch:
            raise ValueError("Required private expansion is absent from the release manifest: " + version)
    if by_version[POLYMER_VERSION].checksum != POLYMER_CHECKSUM:
        raise ValueError("Optional polymer MD migration differs from the approved release")
    if by_version[LITERATURE_VERSION].checksum != LITERATURE_MIGRATION_SHA256:
        raise ValueError("Optional literature migration differs from the approved release")
    boundaries = ([ENTITLEMENT_VERSION, POLYMER_VERSION, LITERATURE_VERSION] if require_entitlements else
                  [USAGE_VERSION, ENTITLEMENT_VERSION, POLYMER_VERSION, LITERATURE_VERSION] if require_usage else
                  [CURRENT_VERSION, LIBRARY_VERSION, USAGE_VERSION, ENTITLEMENT_VERSION, POLYMER_VERSION, LITERATURE_VERSION])
    accepted = [[{"version": entry.version, "checksum": entry.checksum}
                 for entry in entries if entry.version <= boundary] for boundary in boundaries]
    if observed not in accepted:
        raise ValueError("Application requires an exact canonical release-known 0019/0020/0021/0022/0023/0024 ledger")
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
