from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT, Settings
from app.migration_policy import (
    MigrationKind,
    assert_pending_migrations_allowed,
    validate_migration_manifest,
)
from app.postgres_database import postgres_connection

MIGRATIONS_DIR = PROJECT_ROOT / "backend" / "migrations" / "postgres"


@dataclass(frozen=True, slots=True)
class MigrationResult:
    version: str
    checksum: str
    applied: bool


def migration_checksum(path: Path) -> str:
    normalized = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def ensure_migration_table(connection) -> None:
    connection.execute("CREATE SCHEMA IF NOT EXISTS governance")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS governance.schema_migrations (
          version text PRIMARY KEY,
          checksum text NOT NULL,
          applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def applied_migrations(connection) -> dict[str, str]:
    ensure_migration_table(connection)
    rows = connection.execute(
        "SELECT version, checksum FROM governance.schema_migrations ORDER BY version"
    ).fetchall()
    return {str(row["version"]): str(row["checksum"]) for row in rows}


def database_is_fresh_for_bootstrap(connection) -> bool:
    """Return true only for a pristine cluster or the explicit empty-ledger case.

    Merely having no tables is insufficient: a partially initialized schema,
    view, sequence, custom type, or routine is evidence that the database is
    already owned.  ``public`` may remain empty, while ``governance`` is only
    tolerated when its sole application relation is an empty, regular
    ``schema_migrations`` table.
    """

    state = connection.execute(
        """
        SELECT
          to_regclass('governance.schema_migrations') AS ledger_relation,
          EXISTS (
            SELECT 1
            FROM pg_namespace
            WHERE nspname = 'governance'
          ) AS governance_schema_exists,
          EXISTS (
            SELECT 1
            FROM pg_namespace
            WHERE nspname NOT IN ('pg_catalog', 'information_schema', 'public', 'governance')
              AND nspname !~ '^pg_(toast|temp)(_|$)'
          ) AS unexpected_schema_exists,
          EXISTS (
            SELECT 1
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname IN ('public', 'governance')
              AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
              AND NOT (
                n.nspname = 'governance'
                AND c.relname = 'schema_migrations'
                AND c.relkind = 'r'
              )
          ) AS application_relation_exists,
          EXISTS (
            SELECT 1
            FROM pg_type AS t
            JOIN pg_namespace AS n ON n.oid = t.typnamespace
            WHERE n.nspname IN ('public', 'governance')
              AND t.typtype IN ('c', 'd', 'e', 'r', 'm')
              AND NOT EXISTS (
                SELECT 1
                FROM pg_class AS ledger
                WHERE ledger.oid = t.typrelid
                  AND n.nspname = 'governance'
                  AND ledger.relname = 'schema_migrations'
                  AND ledger.relkind = 'r'
              )
          ) AS application_type_exists,
          EXISTS (
            SELECT 1
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname IN ('public', 'governance')
          ) AS application_routine_exists
        """
    ).fetchone()
    if state is None:
        return False

    ledger_relation = state["ledger_relation"]
    governance_schema_exists = bool(state["governance_schema_exists"])
    if governance_schema_exists != (ledger_relation is not None):
        # An empty governance schema is still a partially initialized managed
        # schema; conversely, a relation cannot validly exist without it.
        return False

    if ledger_relation is not None:
        recorded = connection.execute(
            "SELECT 1 FROM governance.schema_migrations LIMIT 1"
        ).fetchone()
        if recorded is not None:
            return False

    return not any(
        bool(state[field])
        for field in (
            "unexpected_schema_exists",
            "application_relation_exists",
            "application_type_exists",
            "application_routine_exists",
        )
    )


def apply_postgres_migrations(
    dsn: str,
    migrations_dir: Path = MIGRATIONS_DIR,
    *,
    allowed_kinds: set[MigrationKind] | None = None,
    allow_contract_on_fresh_database: bool = False,
    defer_trailing_contracts: bool = False,
) -> list[MigrationResult]:
    migration_paths = sorted(migrations_dir.glob("*.sql"))
    if not migration_paths:
        raise FileNotFoundError(f"No Postgres migrations found in {migrations_dir}")
    migration_kinds = validate_migration_manifest(migrations_dir)

    results: list[MigrationResult] = []
    with postgres_connection(dsn) as connection:
        fresh_bootstrap = (
            allow_contract_on_fresh_database
            and database_is_fresh_for_bootstrap(connection)
        )
        applied = applied_migrations(connection)
        if allowed_kinds is not None:
            effective_allowed_kinds = set(allowed_kinds)
            if fresh_bootstrap:
                effective_allowed_kinds.add("contract")
            pending_versions = [path.stem for path in migration_paths if path.stem not in applied]
            if defer_trailing_contracts:
                deferred = [
                    version
                    for version in pending_versions
                    if migration_kinds[version] not in effective_allowed_kinds
                ]
                if any(migration_kinds[version] != "contract" for version in deferred):
                    raise RuntimeError("Only contract migrations may be deferred")
                if deferred:
                    first_deferred = pending_versions.index(deferred[0])
                    if any(
                        migration_kinds[version] in effective_allowed_kinds
                        for version in pending_versions[first_deferred + 1 :]
                    ):
                        raise RuntimeError(
                            "Deferred contract migrations must form the trailing pending migration suffix"
                        )
            else:
                assert_pending_migrations_allowed(
                    pending_versions,
                    migration_kinds,
                    effective_allowed_kinds,
                )
        for path in migration_paths:
            version = path.stem
            checksum = migration_checksum(path)
            existing_checksum = applied.get(version)
            if existing_checksum is not None:
                if existing_checksum != checksum:
                    raise RuntimeError(
                        f"Migration {version} was already applied with checksum {existing_checksum}, "
                        f"but local checksum is {checksum}."
                    )
                results.append(MigrationResult(version=version, checksum=checksum, applied=False))
                continue

            if (
                allowed_kinds is not None
                and migration_kinds[version] not in effective_allowed_kinds
                and defer_trailing_contracts
            ):
                results.append(MigrationResult(version=version, checksum=checksum, applied=False))
                continue

            sql = path.read_text(encoding="utf-8")
            with connection.transaction():
                connection.execute(sql)
                connection.execute(
                    """
                    INSERT INTO governance.schema_migrations (version, checksum)
                    VALUES (%s, %s)
                    """,
                    (version, checksum),
                )
            results.append(MigrationResult(version=version, checksum=checksum, applied=True))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply PolyProp Postgres governance migrations.")
    parser.add_argument("--dsn", default=Settings().app_postgres_dsn)
    parser.add_argument("--migrations-dir", type=Path, default=MIGRATIONS_DIR)
    parser.add_argument(
        "--mode",
        choices=("bootstrap", "bootstrap-expand", "restore-expand", "expand", "contract"),
        default="expand",
        help=(
            "bootstrap initializes a fresh database; bootstrap-expand is the one-time "
            "controller cutover mode that defers a trailing contract suffix; restore-expand "
            "uses the same policy only for isolated backup verification; automated releases "
            "use expand and also defer a trailing contract suffix; contract approval permits "
            "expand+contract."
        ),
    )
    args = parser.parse_args()

    allowed_kinds: set[MigrationKind]
    if args.mode in {"bootstrap", "bootstrap-expand", "restore-expand"}:
        allowed_kinds = {"baseline", "expand"}
    elif args.mode == "contract":
        allowed_kinds = {"expand", "contract"}
    else:
        allowed_kinds = {"expand"}
    results = apply_postgres_migrations(
        args.dsn,
        args.migrations_dir,
        allowed_kinds=allowed_kinds,
        allow_contract_on_fresh_database=args.mode == "bootstrap",
        defer_trailing_contracts=args.mode in {
            "bootstrap-expand",
            "restore-expand",
            "expand",
        },
    )
    for result in results:
        status = "applied" if result.applied else "skipped"
        print(f"{result.version}\t{status}\t{result.checksum}")


if __name__ == "__main__":
    main()
