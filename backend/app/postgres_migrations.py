from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT, Settings
from app.postgres_database import postgres_connection

MIGRATIONS_DIR = PROJECT_ROOT / "backend" / "migrations" / "postgres"


@dataclass(frozen=True, slots=True)
class MigrationResult:
    version: str
    checksum: str
    applied: bool


def migration_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def apply_postgres_migrations(dsn: str, migrations_dir: Path = MIGRATIONS_DIR) -> list[MigrationResult]:
    migration_paths = sorted(migrations_dir.glob("*.sql"))
    if not migration_paths:
        raise FileNotFoundError(f"No Postgres migrations found in {migrations_dir}")

    results: list[MigrationResult] = []
    with postgres_connection(dsn) as connection:
        applied = applied_migrations(connection)
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
    args = parser.parse_args()

    results = apply_postgres_migrations(args.dsn, args.migrations_dir)
    for result in results:
        status = "applied" if result.applied else "skipped"
        print(f"{result.version}\t{status}\t{result.checksum}")


if __name__ == "__main__":
    main()
