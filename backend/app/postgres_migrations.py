from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT, Settings
from app.migration_policy import (
    MIGRATION_CHECKSUM_PATTERN,
    MIGRATION_VERSION_PATTERN,
    MigrationKind,
    assert_pending_migrations_allowed,
    canonical_migration_checksum,
    validate_migration_manifest_entries,
)
from app.postgres_database import postgres_connection

MIGRATIONS_DIR = PROJECT_ROOT / "backend" / "migrations" / "postgres"
POLYTAO_CONTRACT_VERSION = "0012_drop_polytao_jobs"
POLYTAO_CONTRACT_CHECKSUM = "c59b6f1efe9f926ad135379bd1a7141a7920730fa93c0e802646b1b913511728"


@dataclass(frozen=True, slots=True)
class MigrationResult:
    version: str
    checksum: str
    applied: bool


def migration_checksum(path: Path) -> str:
    return canonical_migration_checksum(path)


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
        "SELECT version, checksum FROM governance.schema_migrations ORDER BY version, checksum"
    ).fetchall()
    applied: dict[str, str] = {}
    duplicates: set[str] = set()
    for row in rows:
        version = str(row["version"])
        checksum = str(row["checksum"])
        if MIGRATION_VERSION_PATTERN.fullmatch(version) is None:
            raise RuntimeError(
                f"Migration ledger contains an invalid version identifier: {version!r}"
            )
        if MIGRATION_CHECKSUM_PATTERN.fullmatch(checksum) is None:
            raise RuntimeError(
                f"Migration ledger contains an invalid checksum for {version}"
            )
        if version in applied:
            duplicates.add(version)
            continue
        applied[version] = checksum
    if duplicates:
        raise RuntimeError(
            "Migration ledger contains duplicate versions: "
            + ", ".join(sorted(duplicates))
        )
    return applied


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
    restricted_contract: tuple[str, str] | None = None,
) -> list[MigrationResult]:
    if allowed_kinds is None:
        raise ValueError(
            "Migration callers must select an explicit policy; use CLI --mode bootstrap for a fresh database"
        )
    migration_paths = sorted(migrations_dir.glob("*.sql"))
    if not migration_paths:
        raise FileNotFoundError(f"No Postgres migrations found in {migrations_dir}")
    entries = validate_migration_manifest_entries(migrations_dir)
    entries_by_version = {entry.version: entry for entry in entries}
    migration_kinds = {entry.version: entry.kind for entry in entries}
    if [path.stem for path in migration_paths] != [entry.version for entry in entries]:
        raise RuntimeError("Validated migration entries do not match migration SQL ordering")

    restricted_version: str | None = None
    if restricted_contract is not None:
        if restricted_contract != (
            POLYTAO_CONTRACT_VERSION,
            POLYTAO_CONTRACT_CHECKSUM,
        ):
            raise ValueError("Only the checksum-pinned 0012 contract operation is supported")
        if allow_contract_on_fresh_database or defer_trailing_contracts:
            raise ValueError("A restricted contract cannot use bootstrap or deferred-contract policy")
        restricted_version, expected_checksum = restricted_contract
        entry = entries_by_version.get(restricted_version)
        if (
            entry is None
            or entry.kind != "contract"
            or entry.checksum != expected_checksum
        ):
            raise RuntimeError(
                f"Restricted contract {restricted_version} is absent or has an unexpected canonical checksum"
            )
        if allowed_kinds != {"contract"}:
            raise ValueError("A restricted contract operation may allow only contract migrations")

    results: list[MigrationResult] = []
    with postgres_connection(dsn) as connection:
        fresh_bootstrap = (
            allow_contract_on_fresh_database
            and database_is_fresh_for_bootstrap(connection)
        )
        if (
            restricted_contract is None
            and "contract" in allowed_kinds
            and not fresh_bootstrap
        ):
            raise ValueError(
                "Generic contract execution is forbidden; use the checksum-pinned 0012 operation"
            )
        applied = applied_migrations(connection)

        unknown_versions = sorted(set(applied).difference(entries_by_version))
        if unknown_versions:
            raise RuntimeError(
                "Migration ledger contains versions absent from the canonical manifest: "
                + ", ".join(unknown_versions)
            )

        # Validate the entire known ledger before planning any migration SQL.
        # In particular, a database cannot enter a later epoch using only a
        # matching migration name: every dependency is checksum-bound.
        for entry in entries:
            existing_checksum = applied.get(entry.version)
            if existing_checksum is not None and existing_checksum != entry.checksum:
                raise RuntimeError(
                    f"Migration {entry.version} was already applied with checksum {existing_checksum}, "
                    f"but local checksum is {entry.checksum}."
                )

        if restricted_version is not None:
            target_index = next(
                index for index, entry in enumerate(entries) if entry.version == restricted_version
            )
            canonical_prefix = [entry.version for entry in entries[:target_index]]
            expected_ledger = set(canonical_prefix)
            if restricted_version in applied:
                expected_ledger.add(restricted_version)
            actual_ledger = set(applied)
            if actual_ledger != expected_ledger:
                missing_prerequisites = sorted(expected_ledger.difference(actual_ledger))
                unexpected_versions = sorted(actual_ledger.difference(expected_ledger))
                detail = []
                if missing_prerequisites:
                    detail.append("missing " + ", ".join(missing_prerequisites))
                if unexpected_versions:
                    detail.append("unexpected " + ", ".join(unexpected_versions))
                raise RuntimeError(
                    f"Restricted contract {restricted_version} requires the exact canonical "
                    f"ledger prefix through {'itself' if restricted_version in applied else 'its predecessor'}: "
                    + "; ".join(detail)
                )

        effective_allowed_kinds: set[MigrationKind] | None = None
        deferred_versions: set[str] = set()
        if allowed_kinds is not None:
            effective_allowed_kinds = set(allowed_kinds)
            if fresh_bootstrap:
                effective_allowed_kinds.add("contract")
            pending_versions = [
                entry.version
                for entry in entries
                if entry.version not in applied
                and (restricted_version is None or entry.version == restricted_version)
            ]
            if defer_trailing_contracts:
                deferred = [
                    version
                    for version in pending_versions
                    if migration_kinds[version] not in effective_allowed_kinds
                ]
                if any(migration_kinds[version] != "contract" for version in deferred):
                    raise RuntimeError("Only contract migrations may be deferred")
                deferred_versions.update(deferred)
                manifest_schema_version = entries[0].manifest_schema_version if entries else 1
                if deferred and manifest_schema_version == 1:
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

        # Plan every action and dependency against a simulated ledger before
        # executing the first migration. This guarantees that an epoch-2
        # expansion cannot partly mutate a database whose required contract is
        # absent or checksum-mismatched.
        simulated = dict(applied)
        will_apply: set[str] = set()
        for entry in entries:
            if entry.version in simulated:
                pass
            elif restricted_version is not None and entry.version != restricted_version:
                continue
            elif entry.version in deferred_versions:
                continue
            elif effective_allowed_kinds is not None and entry.kind not in effective_allowed_kinds:
                continue
            else:
                will_apply.add(entry.version)

            if entry.version in simulated or entry.version in will_apply:
                for requirement in entry.requires_contracts:
                    actual = simulated.get(requirement.version)
                    if actual != requirement.checksum:
                        detail = "missing" if actual is None else f"checksum {actual}"
                        raise RuntimeError(
                            f"Migration {entry.version} requires approved contract "
                            f"{requirement.version} at checksum {requirement.checksum}; found {detail}."
                        )
                if entry.version in will_apply:
                    simulated[entry.version] = str(entry.checksum)

        for path in migration_paths:
            version = path.stem
            checksum = str(entries_by_version[version].checksum)
            existing_checksum = applied.get(version)
            if existing_checksum is not None:
                results.append(MigrationResult(version=version, checksum=checksum, applied=False))
                continue

            if version not in will_apply:
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


def apply_polytao_contract_migration(
    dsn: str,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[MigrationResult]:
    """Apply only the reviewed 0012 contract at its immutable checksum."""

    return apply_postgres_migrations(
        dsn,
        migrations_dir,
        allowed_kinds={"contract"},
        restricted_contract=(POLYTAO_CONTRACT_VERSION, POLYTAO_CONTRACT_CHECKSUM),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply PolyProp Postgres governance migrations.")
    parser.add_argument("--dsn", default=Settings().app_postgres_dsn)
    parser.add_argument("--migrations-dir", type=Path, default=MIGRATIONS_DIR)
    parser.add_argument(
        "--mode",
        choices=(
            "bootstrap",
            "bootstrap-expand",
            "restore-expand",
            "expand",
            "contract-0012",
        ),
        default="expand",
        help=(
            "bootstrap initializes a fresh database; bootstrap-expand is the one-time "
            "controller cutover mode that defers a trailing contract suffix; restore-expand "
            "uses the same policy only for isolated backup verification; automated releases "
            "use expand and also defer a trailing contract suffix; contract-0012 is the only "
            "exposed destructive operation and is checksum-pinned."
        ),
    )
    args = parser.parse_args()

    if args.mode == "contract-0012":
        results = apply_polytao_contract_migration(args.dsn, args.migrations_dir)
        for result in results:
            status = "applied" if result.applied else "skipped"
            print(f"{result.version}\t{status}\t{result.checksum}")
        return

    allowed_kinds: set[MigrationKind]
    if args.mode in {"bootstrap", "bootstrap-expand", "restore-expand"}:
        allowed_kinds = {"baseline", "expand"}
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
