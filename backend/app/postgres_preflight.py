from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

from app.config import Settings
from app.migration_compatibility import compatible_forward_versions
from app.migration_policy import validate_migration_manifest_entries
from app.postgres_database import PostgresUnavailableError, postgres_connection
from app.services.monomer_dft_schema import (
    MONOMER_DFT_MIGRATION_VERSION,
    MonomerDftSchemaState,
    probe_monomer_dft_schema,
)

SQLITE_TABLES = {
    "main": ["polymers", "properties", "knowledge_documents", "online_knowledge_history", "online_knowledge_jobs"],
    "pi": ["pi_candidates", "smiles_iupac_cache"],
    "dft": ["dft_molecule_final", "dft_energy_trace"],
}

POSTGRES_TABLES = [
    ("governance", "source_files"),
    ("governance", "import_batches"),
    ("governance", "deployment_control"),
    ("governance", "database_analytics_snapshots"),
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
]
MONOMER_DFT_POSTGRES_TABLES = [
    ("monomer_dft", "jobs"),
    ("monomer_dft", "job_attempts"),
    ("monomer_dft", "artifacts"),
]
POSTGRES_TABLES = [*POSTGRES_TABLES, *MONOMER_DFT_POSTGRES_TABLES]

_MIGRATION_MANIFEST = (
    Path(__file__).resolve().parents[1] / "migrations" / "postgres" / "manifest.json"
)
_MIGRATION_POLICY = validate_migration_manifest_entries(_MIGRATION_MANIFEST.parent)
_MIGRATION_CHECKSUMS = {
    migration.version: str(migration.checksum) for migration in _MIGRATION_POLICY
}
_EXPAND_MIGRATIONS = tuple(
    migration.version
    for migration in _MIGRATION_POLICY
    if migration.kind in {"baseline", "expand"}
)
_REQUIRED_CONTRACT_DEPENDENCIES = frozenset(
    requirement.version
    for migration in _MIGRATION_POLICY
    if migration.kind in {"baseline", "expand"}
    for requirement in migration.requires_contracts
)
STRICT_REQUIRED_MIGRATIONS = tuple(
    migration.version
    for migration in _MIGRATION_POLICY
    if (
        migration.version in _EXPAND_MIGRATIONS
        or migration.version in _REQUIRED_CONTRACT_DEPENDENCIES
    )
)
STARTUP_REQUIRED_MIGRATIONS = tuple(
    migration.version
    for migration in _MIGRATION_POLICY
    if migration.version in STRICT_REQUIRED_MIGRATIONS and migration.epoch == 1
)
KNOWN_CONTRACT_MIGRATIONS = tuple(
    migration.version for migration in _MIGRATION_POLICY if migration.kind == "contract"
)
STRICT_RUNTIME_TABLES = tuple(POSTGRES_TABLES)
STARTUP_RUNTIME_TABLES = tuple(
    table for table in POSTGRES_TABLES if table not in MONOMER_DFT_POSTGRES_TABLES
)
SCHEMA_TARGET_STARTUP = "startup-through-0012"
SCHEMA_TARGET_FINAL = "final-0013"
SCHEMA_TARGETS = frozenset({SCHEMA_TARGET_STARTUP, SCHEMA_TARGET_FINAL})


def _required_migrations(schema_target: str) -> tuple[str, ...]:
    if schema_target == SCHEMA_TARGET_STARTUP:
        return STARTUP_REQUIRED_MIGRATIONS
    if schema_target == SCHEMA_TARGET_FINAL:
        return STRICT_REQUIRED_MIGRATIONS
    raise ValueError(f"unsupported schema target: {schema_target}")


def _required_runtime_tables(
    schema_target: str,
) -> tuple[tuple[str, str], ...]:
    if schema_target == SCHEMA_TARGET_STARTUP:
        return STARTUP_RUNTIME_TABLES
    if schema_target == SCHEMA_TARGET_FINAL:
        return STRICT_RUNTIME_TABLES
    raise ValueError(f"unsupported schema target: {schema_target}")


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


def _postgres_applied_migrations(
    connection,
) -> tuple[dict[str, str], list[dict[str, object]]]:
    if not _postgres_table_exists(connection, "governance", "schema_migrations"):
        return {}, []
    rows = connection.execute(
        "SELECT version, checksum FROM governance.schema_migrations ORDER BY version, checksum"
    ).fetchall()
    applied: dict[str, str] = {}
    checksums_by_version: dict[str, list[str]] = {}
    for row in rows:
        version = str(row["version"])
        checksum = str(row["checksum"])
        checksums_by_version.setdefault(version, []).append(checksum)
        applied.setdefault(version, checksum)
    duplicates = [
        {"version": version, "checksums": checksums}
        for version, checksums in sorted(checksums_by_version.items())
        if len(checksums) > 1
    ]
    return applied, duplicates


SNAPSHOT_ROW_COMPARISONS = {
    "process": ("experimental", "process_records"),
    "property": ("experimental", "property_records"),
    "structureEffect": ("core", "polymer_properties"),
    "propertyFilter": ("core", "polymer_property_filter_records"),
    "dft": ("dft", "energy_trace"),
    "formulation": ("knowledge", "formulation_records"),
}


def _analytics_snapshot_report(connection) -> dict[str, object]:
    try:
        from app.services.analytics_snapshot_store import load_analytics_snapshot

        # Keep the surrounding diagnostic transaction usable after a missing or
        # corrupt table, but never replace production truth with checked-in data.
        with connection.transaction():
            stored_snapshot = load_analytics_snapshot(connection)
    except Exception as exc:
        return {
            "generated_at": None,
            "source_sha": None,
            "source": "postgres-error",
            "comparisons": {},
            "warnings": [f"Postgres analytics snapshot unavailable: {exc}"],
        }
    if stored_snapshot is None:
        return {
            "generated_at": None,
            "source_sha": None,
            "source": "postgres-missing",
            "comparisons": {},
            "warnings": ["Postgres analytics snapshot is missing"],
        }

    snapshot = stored_snapshot.datasets
    generated_at = stored_snapshot.generated_at.isoformat()
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
        "generated_at": generated_at,
        "source_sha": stored_snapshot.source_sha,
        "source": "postgres",
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

    schema_target = report.get("schema_target", SCHEMA_TARGET_FINAL)
    if not isinstance(schema_target, str) or schema_target not in SCHEMA_TARGETS:
        errors.append("Postgres preflight schema target is invalid")
        return errors

    migrations = report.get("migrations")
    if not isinstance(migrations, dict):
        errors.append("Postgres migration status is unavailable")
    else:
        for version in migrations.get("missing", []):
            errors.append(f"Required Postgres migration is missing: {version}")
        for mismatch in migrations.get("checksum_mismatches", []):
            if isinstance(mismatch, dict):
                errors.append(
                    "Postgres migration checksum differs from the canonical source: "
                    f"{mismatch.get('version')}"
                )
        for version in migrations.get("unknown_migrations", []):
            errors.append(
                f"Postgres migration ledger contains an unknown version: {version}"
            )
        for duplicate in migrations.get("duplicate_migrations", []):
            if isinstance(duplicate, dict):
                errors.append(
                    "Postgres migration ledger contains a duplicate version: "
                    f"{duplicate.get('version')}"
                )
        for dependency in migrations.get("dependency_errors", []):
            errors.append(f"Postgres migration contract dependency is not satisfied: {dependency}")

    tables = postgres.get("tables")
    if not isinstance(tables, dict):
        errors.append("Postgres table status is unavailable")
    else:
        for schema, table in _required_runtime_tables(schema_target):
            key = f"{schema}.{table}"
            if tables.get(key) is None:
                errors.append(f"Required Postgres table is missing: {key}")
        if report.get("mode") == "runtime" and tables.get("core.polymer_property_filter_records") == 0:
            errors.append("Property filter records are empty; run the property_filter import before deployment.")

    dft_schema = report.get("monomer_dft_schema")
    dft_state = dft_schema.get("state") if isinstance(dft_schema, dict) else None
    if dft_state == MonomerDftSchemaState.INVALID.value:
        reason = dft_schema.get("reason") if isinstance(dft_schema, dict) else None
        errors.append(
            "Monomer DFT schema is partial or invalid: "
            f"{reason or 'unknown reason'}"
        )
    elif (
        schema_target == SCHEMA_TARGET_FINAL
        and dft_state != MonomerDftSchemaState.READY.value
    ):
        errors.append(
            "Final runtime requires the checksum-exact 0013 monomer DFT schema"
        )

    if report.get("mode") == "runtime":
        files = report.get("files")
        if isinstance(files, dict):
            property_filter_file = files.get("property_filter_csv")
            if isinstance(property_filter_file, dict) and not property_filter_file.get("exists"):
                errors.append("Required runtime source is missing: property_filter_csv")
        analytics = report.get("analytics_snapshot")
        if not isinstance(analytics, dict) or analytics.get("source") != "postgres":
            errors.append("Required Postgres analytics snapshot is missing or invalid")
        else:
            warnings = analytics.get("warnings")
            if not isinstance(warnings, list):
                errors.append("Postgres analytics snapshot validation is unavailable")
            else:
                errors.extend(str(warning) for warning in warnings)
            expected_source_sha = report.get("expected_source_sha")
            if (
                isinstance(expected_source_sha, str)
                and analytics.get("source_sha") != expected_source_sha
            ):
                errors.append(
                    "Postgres analytics snapshot source SHA does not match the running release"
                )

    if report.get("mode") == "migration":
        files = report.get("files")
        if isinstance(files, dict):
            for key, value in files.items():
                if isinstance(value, dict) and not value.get("exists"):
                    errors.append(f"Required migration source is missing: {key}")
    return errors


def preflight_blockers(report: dict[str, object]) -> list[str]:
    return strict_preflight_errors(report)


def run_preflight(
    settings: Settings,
    dsn: str | None = None,
    mode: str = "runtime",
    strict: bool = False,
    expected_source_sha: str | None = None,
    schema_target: str = SCHEMA_TARGET_FINAL,
) -> dict[str, object]:
    if mode not in {"runtime", "migration", "schema"}:
        raise ValueError("mode must be 'runtime', 'migration', or 'schema'")
    if schema_target not in SCHEMA_TARGETS:
        raise ValueError(
            "schema_target must be "
            f"{SCHEMA_TARGET_STARTUP!r} or {SCHEMA_TARGET_FINAL!r}"
        )
    if expected_source_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", expected_source_sha):
        raise ValueError("expected_source_sha must be a full lowercase 40-character SHA")

    required_migrations = _required_migrations(schema_target)
    target_dsn = dsn or settings.app_postgres_dsn
    report: dict[str, object] = {
        "mode": mode,
        "strict": strict,
        "schema_target": schema_target,
        "expected_source_sha": expected_source_sha,
        "structured_data_backend": settings.structured_data_backend,
        "app_postgres_dsn": _safe_dsn_label(target_dsn),
        "postgres": {"reachable": False, "tables": {}},
        "migrations": {
            "required": list(required_migrations),
            "contracts": list(KNOWN_CONTRACT_MIGRATIONS),
            "applied": [],
            "missing": list(required_migrations),
            "pending_contracts": list(KNOWN_CONTRACT_MIGRATIONS),
            "checksum_mismatches": [],
            "dependency_errors": [],
            "unknown_migrations": [],
            "forward_compatible_migrations": [],
            "duplicate_migrations": [],
        },
        "monomer_dft_schema": {
            "state": "unavailable",
            "reason": "postgres_unavailable",
            "catalog_sha256": None,
        },
        "files": {},
    }

    files = {} if mode == "schema" else {
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
            applied_migrations, duplicate_migrations = _postgres_applied_migrations(
                connection
            )
            checksum_mismatches = [
                {
                    "version": version,
                    "expected": expected,
                    "actual": applied_migrations[version],
                }
                for version, expected in _MIGRATION_CHECKSUMS.items()
                if version in applied_migrations and applied_migrations[version] != expected
            ]
            dependency_errors = [
                f"{migration.version} requires {requirement.version}@{requirement.checksum}"
                for migration in _MIGRATION_POLICY
                if migration.version in applied_migrations
                for requirement in migration.requires_contracts
                if applied_migrations.get(requirement.version) != requirement.checksum
            ]
            forward_compatible_migrations = sorted(
                compatible_forward_versions(
                    applied_migrations,
                    _MIGRATION_CHECKSUMS,
                )
            )
            unknown_migrations = sorted(
                set(applied_migrations)
                .difference(_MIGRATION_CHECKSUMS)
                .difference(forward_compatible_migrations)
            )
            dft_schema = probe_monomer_dft_schema(connection)
            table_counts = {
                f"{schema}.{table}": _postgres_count(connection, schema, table)
                for schema, table in STARTUP_RUNTIME_TABLES
            }
            if dft_schema.state is MonomerDftSchemaState.READY:
                table_counts.update(
                    {
                        f"{schema}.{table}": _postgres_count(
                            connection,
                            schema,
                            table,
                        )
                        for schema, table in MONOMER_DFT_POSTGRES_TABLES
                    }
                )
            else:
                table_counts.update(
                    {
                        f"{schema}.{table}": None
                        for schema, table in MONOMER_DFT_POSTGRES_TABLES
                    }
                )
            report["postgres"] = {
                "reachable": True,
                "database": version["database"],
                "user": version["user"],
                "version": str(version["version"]).split(",", 1)[0],
                "tables": table_counts,
            }
            report["monomer_dft_schema"] = {
                "state": dft_schema.state.value,
                "reason": dft_schema.reason,
                "catalog_sha256": dft_schema.catalog_sha256,
            }
            report["migrations"] = {
                "required": list(required_migrations),
                "contracts": list(KNOWN_CONTRACT_MIGRATIONS),
                "applied": list(applied_migrations),
                "missing": [
                    version
                    for version in required_migrations
                    if version not in applied_migrations
                ],
                "pending_contracts": [
                    version
                    for version in KNOWN_CONTRACT_MIGRATIONS
                    if version not in applied_migrations
                ],
                "checksum_mismatches": checksum_mismatches,
                "dependency_errors": dependency_errors,
                "unknown_migrations": unknown_migrations,
                "forward_compatible_migrations": forward_compatible_migrations,
                "duplicate_migrations": duplicate_migrations,
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
    parser.add_argument("--mode", choices=["runtime", "migration", "schema"], default="runtime")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when required migrations or runtime tables are missing.")
    parser.add_argument("--expected-source-sha", help="Require the stored analytics snapshot to match this release SHA.")
    parser.add_argument(
        "--schema-target",
        choices=sorted(SCHEMA_TARGETS),
        default=SCHEMA_TARGET_FINAL,
        help="Validate startup compatibility through 0012 or final readiness through 0013.",
    )
    args = parser.parse_args()
    report = run_preflight(
        Settings(),
        args.dsn,
        args.mode,
        strict=args.strict,
        expected_source_sha=args.expected_source_sha,
        schema_target=args.schema_target,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and report["strict_errors"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
