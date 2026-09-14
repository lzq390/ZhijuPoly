"""Load synthetic filter samples into the existing isolated POC database only."""

import argparse
import csv
from hashlib import sha256
from pathlib import Path
import sys

import psycopg
from dotenv import dotenv_values
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.services.property_filter_catalog import rebuild_property_filter_catalog

SOURCE = "scripts/fixtures/property_filter_poc.csv"
NOTICE = "Synthetic POC demo data; not experimental measurements or literature evidence."


def load_samples():
    with (ROOT / SOURCE).open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    for index, row in enumerate(rows, 1):
        for key in row:
            row[key] = row[key] or None
        row["filter_record_id"] = int(row["filter_record_id"])
        for key in ("property_value_num", "canonical_value"):
            if row[key] is not None:
                row[key] = float(row[key])
        row.update(source_file=SOURCE, source_row_number=index,
                   property_value=str(row["property_value_num"]),
                   property_unit_raw=row["property_unit_clean"],
                   value_origin="synthetic_poc", label_source="synthetic_poc",
                   soft_quality_flags=NOTICE)
    return rows


def initialize(connection):
    migrations = ROOT / "backend/migrations/postgres"
    # Reuse only required existing schemas/tables; do not run platform migrations.
    prefixes = (
        "CREATE SCHEMA IF NOT EXISTS core;", "CREATE SCHEMA IF NOT EXISTS governance;",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm;",
        "CREATE TABLE IF NOT EXISTS governance.source_files (",
        "CREATE TABLE IF NOT EXISTS governance.import_batches (",
    )
    for part in (migrations / "0001_app_data_governance.sql").read_text().split(";"):
        statement = part.strip() + ";"
        if statement.startswith(prefixes):
            connection.execute(statement)
    connection.execute((migrations / "0006_property_filter_records.sql").read_text())

    samples = load_samples()
    columns = list(samples[0])
    names = sql.SQL(", ").join(map(sql.Identifier, columns))
    insert = sql.SQL("INSERT INTO core.polymer_property_filter_records ({}) VALUES ({}) "
                     "ON CONFLICT (filter_record_id) DO NOTHING").format(
        names, sql.SQL(", ").join(sql.Placeholder() for _ in columns))
    for sample in samples:
        connection.execute(insert, [sample[key] for key in columns])
        existing = connection.execute(sql.SQL(
            "SELECT {} FROM core.polymer_property_filter_records WHERE filter_record_id = %s"
        ).format(names), (sample["filter_record_id"],)).fetchone()
        if existing != sample:
            raise RuntimeError("Sample ID already contains different data; transaction rolled back")

    digest = sha256((ROOT / SOURCE).read_bytes()).hexdigest()
    connection.execute("""
        INSERT INTO governance.source_files (logical_name, path, status, row_count, sha256, notes)
        VALUES ('property_filter_csv', %s, 'ready', %s, %s, %s)
        ON CONFLICT (logical_name) DO NOTHING
    """, (SOURCE, len(samples), digest, NOTICE))
    source = connection.execute("SELECT * FROM governance.source_files WHERE logical_name = 'property_filter_csv'").fetchone()
    if source["path"] != SOURCE or source["sha256"] != digest:
        raise RuntimeError("Filter source already exists with different data; transaction rolled back")
    batch = connection.execute("""
        SELECT import_batch_id FROM governance.import_batches
        WHERE dataset_key = 'property_filter' AND source_file_id = %s AND status = 'completed'
        ORDER BY import_batch_id DESC LIMIT 1
    """, (source["source_file_id"],)).fetchone()
    if batch is None:
        batch = connection.execute("""
            INSERT INTO governance.import_batches
              (dataset_key, source_file_id, finished_at, status, row_count)
            VALUES ('property_filter', %s, now(), 'completed', %s) RETURNING import_batch_id
        """, (source["source_file_id"], len(samples))).fetchone()
    connection.execute((migrations / "0015_property_filter_performance.sql").read_text())
    rebuild_property_filter_catalog(connection, import_batch_id=batch["import_batch_id"], source_sha256=digest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="Create required tables and insert samples; otherwise only inspect")
    args = parser.parse_args()
    # Ignore model/production settings; use only this development database file.
    dsn = dotenv_values(ROOT / ".runtime/knowledge-summary/database.env").get("APP_POSTGRES_DSN")
    if not dsn:
        raise RuntimeError("Missing APP_POSTGRES_DSN in the POC database.env")
    target = conninfo_to_dict(dsn)
    if (target.get("host"), target.get("port"), target.get("dbname"), target.get("user")) != (
        "127.0.0.1", "15432", "knowledge_poc", "knowledge_poc"
    ) or target.get("service") or target.get("hostaddr"):
        raise RuntimeError("Refusing a target other than the isolated local POC database")
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        if args.init:
            initialize(connection)
        else:
            connection.execute("SET TRANSACTION READ ONLY")
        counts = connection.execute("""
            SELECT count(*) AS measurements, count(DISTINCT smiles) AS materials
            FROM core.polymer_property_filter_records WHERE source_file = %s
        """, (SOURCE,)).fetchone()
    print(dict(counts))
    print(NOTICE)


if __name__ == "__main__":
    main()
