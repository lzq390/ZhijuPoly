"""Lightweight MCP tests: only an explicitly configured disposable PostgreSQL.

These fixtures intentionally do not inherit backend/tests/conftest.py or load
application settings, scientific models, or an application .env file.
"""
from __future__ import annotations

import csv
import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
import pytest

from app.services.property_filter_catalog import rebuild_property_filter_catalog

ROOT = Path(__file__).resolve().parents[2]
TABLES = (
    "core.polymer_property_filter_records",
    "governance.property_filter_options_snapshots",
    "governance.import_batches",
    "governance.source_files",
)


@dataclass(frozen=True)
class TestDatabase:
    admin_dsn: str
    reader_dsn: str
    reader_role: str

    def connect(self):
        return psycopg.connect(self.admin_dsn, row_factory=dict_row)

    def refresh_catalog(self):
        with self.connect() as connection:
            batch = connection.execute(
                "SELECT MAX(import_batch_id) AS id FROM governance.import_batches"
            ).fetchone()["id"]
            return rebuild_property_filter_catalog(
                connection, import_batch_id=batch, source_sha256="a" * 64
            )


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
def test_database():
    filename = os.getenv("NEXPOLY_MCP_TEST_ADMIN_DSN_FILE")
    if not filename:
        pytest.fail("Set NEXPOLY_MCP_TEST_ADMIN_DSN_FILE to a disposable MCP test PostgreSQL; there is no fallback.")
    base_dsn = Path(filename).read_text().strip()
    target = conninfo_to_dict(base_dsn)
    if (target.get("dbname") != "mcp_test_admin" or target.get("user") != "mcp_test_admin"
            or target.get("host") not in {"127.0.0.1", "mcp-test-postgres"}
            or any(target.get(key) for key in ("service", "hostaddr"))):
        pytest.fail("Refusing a database other than the explicitly isolated MCP test server.")
    suffix = uuid4().hex[:12]
    database_name, reader = f"mcp_test_{suffix}", f"mcp_reader_{suffix}"
    password = secrets.token_urlsafe(32)
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(sql.Identifier(reader), sql.Literal(password)))
        admin.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(sql.Identifier(reader)))
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database_name)))
    admin_dsn = make_conninfo(base_dsn, dbname=database_name)
    reader_dsn = make_conninfo(admin_dsn, user=reader, password=password)
    try:
        with psycopg.connect(admin_dsn, row_factory=dict_row) as connection:
            baseline = (ROOT / "backend/migrations/postgres/0001_app_data_governance.sql").read_text()
            prefixes = ("CREATE SCHEMA IF NOT EXISTS core;", "CREATE SCHEMA IF NOT EXISTS governance;",
                        "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
                        "CREATE TABLE IF NOT EXISTS governance.source_files (",
                        "CREATE TABLE IF NOT EXISTS governance.import_batches (")
            for part in baseline.split(";"):
                statement = part.strip() + ";"
                if statement.startswith(prefixes):
                    connection.execute(statement)
            for migration in ("0006_property_filter_records.sql", "0015_property_filter_performance.sql"):
                connection.execute((ROOT / "backend/migrations/postgres" / migration).read_text())
            connection.execute("CREATE TABLE core.private_test_data (secret text)")
            connection.execute("INSERT INTO core.private_test_data VALUES ('not MCP data')")
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database_name), sql.Identifier(reader)))
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA core, governance TO {}").format(sql.Identifier(reader)))
            for table in TABLES:
                connection.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(*table.split(".")), sql.Identifier(reader)))
        yield TestDatabase(admin_dsn, reader_dsn, reader)
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database_name)))
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(reader)))


@pytest.fixture
def seeded_db(test_database):
    fixture = ROOT / "scripts/fixtures/property_filter_poc.csv"
    with fixture.open() as stream:
        records = list(csv.DictReader(stream))
    with test_database.connect() as connection:
        connection.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY")
        for index, record in enumerate(records, 1):
            row = {key: value or None for key, value in record.items()}
            row.update(source_file="synthetic_mcp_fixture", source_row_number=index,
                       property_value=str(row["property_value_num"]), value_origin="synthetic_poc",
                       label_source="synthetic_poc", soft_quality_flags="Synthetic test data, not experimental evidence.")
            columns = list(row)
            connection.execute(sql.SQL("INSERT INTO core.polymer_property_filter_records ({}) VALUES ({})").format(
                sql.SQL(",").join(map(sql.Identifier, columns)),
                sql.SQL(",").join(sql.Placeholder() for _ in columns),
            ), [row[key] for key in columns])
        source = connection.execute("""INSERT INTO governance.source_files
            (logical_name,path,status,row_count,sha256,notes) VALUES
            ('property_filter_csv','synthetic_mcp_fixture','ready',%s,%s,'PRIVATE_OPERATION_NOTE')
            RETURNING source_file_id""", (len(records), hashlib.sha256(fixture.read_bytes()).hexdigest())).fetchone()
        batch = connection.execute("""INSERT INTO governance.import_batches
            (dataset_key,source_file_id,status,row_count,finished_at) VALUES
            ('property_filter',%s,'completed',%s,now()) RETURNING import_batch_id""",
            (source["source_file_id"], len(records))).fetchone()
        rebuild_property_filter_catalog(connection, import_batch_id=batch["import_batch_id"], source_sha256="a" * 64)
    return test_database
