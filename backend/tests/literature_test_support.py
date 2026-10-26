"""Explicit optional-DDL installation for disposable PostgreSQL test databases.

The production runner continues to defer both optional migrations. Only the
cluster-wide NOLOGIN maintenance role is namespaced here; SQL checksums,
constraints, triggers and runtime grants are otherwise exercised unchanged.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.auth.isolation_ledger import expected_isolation_ledger
from app.literature_contract import LITERATURE_MIGRATION
from app.migration_policy import validate_migration_manifest_entries

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations/postgres"


def literature_test_role(connection) -> str:
    database = connection.info.dbname
    if not re.fullmatch(r"(?:auth_test_[0-9a-f]{12}|zhijupoly_test_[0-9]+_[0-9a-f]{8})", database):
        raise ValueError("Optional test DDL requires a disposable fixture database")
    return "literature_test_" + hashlib.sha256(database.encode()).hexdigest()[:20]


def _pinned_sql(version: str) -> tuple[str, str]:
    entries = {entry.version: entry for entry in validate_migration_manifest_entries(MIGRATIONS)}
    entry = entries[version]
    assert entry.kind == "expand"
    path = MIGRATIONS / (version + ".sql")
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry.checksum
    return data.decode("utf-8"), entry.checksum


def literature_role_guard(connection) -> str:
    source, _ = _pinned_sql(LITERATURE_MIGRATION)
    begin = source.index("DO $$ BEGIN\n  -- Attribute checks")
    end = source.index("END $$;", begin) + len("END $$;")
    return source[begin:end].replace("nexpoly_literature_import", literature_test_role(connection))


def install_literature_test_projection(dsn: str, *, full_ledger: bool = False) -> None:
    with psycopg.connect(dsn, row_factory=dict_row) as connection:
        role = literature_test_role(connection)
        if full_ledger:
            observed = [dict(row) for row in connection.execute(
                "SELECT version,checksum FROM governance.schema_migrations ORDER BY version")]
            accepted = [expected_isolation_ledger(version) for version in (
                "0022_usage_entitlements", "0023_polymer_md_jobs", LITERATURE_MIGRATION)]
            if observed not in accepted:
                raise ValueError("Test DDL requires the complete canonical migration prefix")
            if observed == accepted[0]:
                source, checksum = _pinned_sql("0023_polymer_md_jobs")
                connection.execute(source)
                connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)",
                                   ("0023_polymer_md_jobs", checksum))
        present = connection.execute("SELECT to_regclass('knowledge.literature_generations') AS value").fetchone()["value"]
        if present is None:
            source, checksum = _pinned_sql(LITERATURE_MIGRATION)
            # A fresh per-database role preserves the fail-closed CREATE ROLE
            # check when several isolated databases share one CI PostgreSQL.
            connection.execute(source.replace("nexpoly_literature_import", role))
            if full_ledger:
                connection.execute("INSERT INTO governance.schema_migrations(version,checksum) VALUES(%s,%s)",
                                   (LITERATURE_MIGRATION, checksum))
        elif full_ledger:
            observed = [dict(row) for row in connection.execute(
                "SELECT version,checksum FROM governance.schema_migrations ORDER BY version")]
            assert observed == expected_isolation_ledger(LITERATURE_MIGRATION)
