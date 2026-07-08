from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

from app.config import Settings
from app.postgres_database import PostgresUnavailableError, postgres_connection

SQLITE_TABLES = {
    "main": ["polymers", "properties", "knowledge_documents", "online_knowledge_history", "online_knowledge_jobs"],
    "pi": ["pi_candidates", "smiles_iupac_cache"],
    "dft": ["dft_molecule_final", "dft_energy_trace"],
}

POSTGRES_TABLES = [
    ("governance", "source_files"),
    ("governance", "import_batches"),
    ("core", "polymers"),
    ("core", "polymer_properties"),
    ("core", "polymer_property_filter_records"),
    ("knowledge", "documents"),
    ("knowledge", "formulation_records"),
    ("online_knowledge", "history"),
    ("online_knowledge", "jobs"),
    ("pi", "polymers"),
    ("pi", "tg_predictions"),
    ("pi", "monomer_iupac"),
    ("dft", "molecule_final"),
    ("dft", "energy_trace"),
    ("experimental", "process_records"),
    ("experimental", "property_records"),
    ("lab", "test_projects"),
    ("lab", "sample_measurements"),
    ("model_registry", "assets"),
    ("md", "monomer_md_jobs"),
    ("generation", "polytao_jobs"),
]

STRICT_REQUIRED_MIGRATIONS = (
    "0001_app_data_governance",
    "0002_lab_identity_defaults",
    "0003_runtime_postgres_cutover",
    "0004_monomer_md_jobs",
    "0005_byteff2_formal_monomer_md",
    "0006_property_filter_records",
    "0007_polytao_jobs",
)
STRICT_RUNTIME_TABLES = tuple(POSTGRES_TABLES)


def _safe_dsn_label(dsn: str) -> str:
    parsed = urlparse(dsn)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/") if parsed.path else ""
    return f"{parsed.scheme}://{host}{port}/{database}"


def _sqlite_count(db_path: Path, table: str) -> int | None:
    if not db_path.exists():
        return None
    connection = sqlite3.connect(db_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            return None
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _postgres_table_exists(connection, schema: str, table: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    ).fetchone()
    return row is not None


def _postgres_count(connection, schema: str, table: str) -> int | None:
    if not _postgres_table_exists(connection, schema, table):
        return None
    row = connection.execute(f'SELECT COUNT(*) AS count FROM "{schema}"."{table}"').fetchone()
    return int(row["count"])


def _postgres_applied_migrations(connection) -> list[str]:
    if not _postgres_table_exists(connection, "governance", "schema_migrations"):
        return []
    rows = connection.execute(
        "SELECT version FROM governance.schema_migrations ORDER BY version"
    ).fetchall()
    return [str(row["version"]) for row in rows]


SNAPSHOT_ROW_COMPARISONS = {
    "process": ("experimental", "process_records"),
    "property": ("experimental", "property_records"),
    "structureEffect": ("core", "polymer_properties"),
    "dft": ("dft", "energy_trace"),
    "formulation": ("knowledge", "formulation_records"),
}


def _analytics_snapshot_report(connection) -> dict[str, object]:
    try:
        from app.services.database_analytics_snapshot import (
            STATIC_DATABASE_ANALYTICS_GENERATED_AT,
            get_database_analytics_snapshot,
        )
    except Exception as exc:  # pragma: no cover - runtime diagnostic fallback
        return {"generated_at": None, "comparisons": {}, "warnings": [f"analytics snapshot unavailable: {exc}"]}

    snapshot = get_database_analytics_snapshot()
    comparisons: dict[str, dict[str, object]] = {}
    warnings: list[str] = []
    for dataset_key, (schema, table) in SNAPSHOT_ROW_COMPARISONS.items():
        snapshot_rows = snapshot.get(dataset_key, {}).get("rows")
        postgres_rows = _postgres_count(connection, schema, table)
        matches = snapshot_rows == postgres_rows
        comparisons[dataset_key] = {
            "snapshot_rows": snapshot_rows,
            "postgres_rows": postgres_rows,
            "matches": matches,
        }
        if not matches:
            warnings.append(f"analytics snapshot row count mismatch for {dataset_key}: snapshot={snapshot_rows} postgres={postgres_rows}")
    return {
        "generated_at": STATIC_DATABASE_ANALYTICS_GENERATED_AT,
        "comparisons": comparisons,
        "warnings": warnings,
    }


def strict_preflight_errors(report: dict[str, object]) -> list[str]:
    errors: list[str] = []
    postgres = report.get("postgres")
    if not isinstance(postgres, dict) or not postgres.get("reachable"):
        detail = postgres.get("error") if isinstance(postgres, dict) else None
        errors.append(f"PostgreSQL database is not reachable: {detail or 'unknown error'}")
        return errors

    migrations = report.get("migrations")
    if not isinstance(migrations, dict):
        errors.append("Postgres migration status is unavailable")
    else:
        for version in migrations.get("missing", []):
            errors.append(f"Required Postgres migration is missing: {version}")

    tables = postgres.get("tables")
    if not isinstance(tables, dict):
        errors.append("Postgres table status is unavailable")
    else:
        for schema, table in STRICT_RUNTIME_TABLES:
            key = f"{schema}.{table}"
            if tables.get(key) is None:
                errors.append(f"Required Postgres table is missing: {key}")

    if report.get("mode") == "migration":
        files = report.get("files")
        if isinstance(files, dict):
            for key, value in files.items():
                if isinstance(value, dict) and not value.get("exists"):
                    errors.append(f"Required migration source is missing: {key}")
    return errors


def preflight_blockers(report: dict[str, object]) -> list[str]:
    return strict_preflight_errors(report)


def run_preflight(settings: Settings, dsn: str | None = None, mode: str = "runtime", strict: bool = False) -> dict[str, object]:
    if mode not in {"runtime", "migration"}:
        raise ValueError("mode must be 'runtime' or 'migration'")

    target_dsn = dsn or settings.app_postgres_dsn
    report: dict[str, object] = {
        "mode": mode,
        "strict": strict,
        "structured_data_backend": settings.structured_data_backend,
        "app_postgres_dsn": _safe_dsn_label(target_dsn),
        "postgres": {"reachable": False, "tables": {}},
        "migrations": {"required": list(STRICT_REQUIRED_MIGRATIONS), "applied": [], "missing": list(STRICT_REQUIRED_MIGRATIONS)},
        "files": {},
    }

    files = {
        "csv_source": settings.csv_source_file,
        "property_filter_csv": settings.property_filter_csv_file,
        "knowledge_zip": settings.knowledge_zip_file,
        "experimental_process_csv": settings.experimental_process_csv_file,
        "experimental_property_csv": settings.experimental_property_csv_file,
    }
    if mode == "migration":
        files.update(
            {
                "main_sqlite": settings.legacy_main_sqlite_source_file,
                "pi_sqlite": settings.legacy_pi_sqlite_source_file,
                "dft_sqlite": settings.legacy_dft_sqlite_source_file,
            }
        )
    report["files"] = {
        key: {
            "path": str(path),
            "exists": path.exists() or path.is_symlink(),
            "bytes": path.lstat().st_size if path.is_symlink() else (path.stat().st_size if path.exists() and path.is_file() else None),
        }
        for key, path in files.items()
    }

    if mode == "migration":
        sqlite_sources: dict[str, dict[str, int | None]] = {}
        for source_key, tables in SQLITE_TABLES.items():
            db_path = {
                "main": settings.legacy_main_sqlite_source_file,
                "pi": settings.legacy_pi_sqlite_source_file,
                "dft": settings.legacy_dft_sqlite_source_file,
            }[source_key]
            sqlite_sources[source_key] = {table: _sqlite_count(db_path, table) for table in tables}
        report["sqlite_sources"] = sqlite_sources

    try:
        with postgres_connection(target_dsn) as connection:
            version = connection.execute("SELECT current_database() AS database, current_user AS user, version() AS version").fetchone()
            applied_migrations = _postgres_applied_migrations(connection)
            report["postgres"] = {
                "reachable": True,
                "database": version["database"],
                "user": version["user"],
                "version": str(version["version"]).split(",", 1)[0],
                "tables": {
                    f"{schema}.{table}": _postgres_count(connection, schema, table)
                    for schema, table in POSTGRES_TABLES
                },
            }
            report["migrations"] = {
                "required": list(STRICT_REQUIRED_MIGRATIONS),
                "applied": applied_migrations,
                "missing": [version for version in STRICT_REQUIRED_MIGRATIONS if version not in set(applied_migrations)],
            }
            report["analytics_snapshot"] = _analytics_snapshot_report(connection)
    except PostgresUnavailableError as exc:
        report["postgres"] = {"reachable": False, "error": str(exc), "tables": {}}

    blockers = preflight_blockers(report)
    strict_errors = strict_preflight_errors(report)
    report["status"] = "failed" if blockers else "ok"
    report["blockers"] = blockers
    report["strict_ok"] = not strict_errors
    report["strict_errors"] = strict_errors
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only PolyProp Postgres governance preflight.")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--mode", choices=["runtime", "migration"], default="runtime")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when required migrations or runtime tables are missing.")
    args = parser.parse_args()
    report = run_preflight(Settings(), args.dsn, args.mode, strict=args.strict)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and report["strict_errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
