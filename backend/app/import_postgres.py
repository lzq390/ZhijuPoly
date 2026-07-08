from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from psycopg.types.json import Jsonb

from app.config import PROJECT_ROOT, Settings
from app.generate_database_analytics_snapshot import write_database_analytics_snapshot
from app.import_csv import canonicalize_smiles, normalize_property_unit, parse_float_or_none
from app.model_asset_manifest import iter_model_asset_specs
from app.postgres_database import postgres_connection
from app.postgres_migrations import apply_postgres_migrations


@dataclass(slots=True)
class DatasetImportStats:
    dataset_key: str
    row_count: int = 0
    details: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class PostgresImportStats:
    datasets: list[DatasetImportStats] = field(default_factory=list)

    def add(self, dataset_key: str, row_count: int, **details: int) -> None:
        self.datasets.append(DatasetImportStats(dataset_key=dataset_key, row_count=row_count, details=details))


def _sqlite_rows(db_path: Path, table: str) -> list[sqlite3.Row]:
    if not db_path.exists():
        return []
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            return []
        return list(connection.execute(f"SELECT * FROM {table}"))
    finally:
        connection.close()

def _iter_sqlite_rows(db_path: Path, table: str):
    if not db_path.exists():
        return
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if exists is None:
            return
        cursor = connection.execute(f"SELECT * FROM {table}")
        for row in cursor:
            yield row
    finally:
        connection.close()



def _sha256(path: Path | str) -> str | None:
    if not isinstance(path, Path) or path.is_symlink() or not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_metadata(path: Path | str) -> tuple[str, bool, int | None]:
    if not isinstance(path, Path):
        return str(path), False, None
    exists = path.exists() or path.is_symlink()
    if path.is_symlink():
        return str(path), exists, path.lstat().st_size
    return str(path), exists, path.stat().st_size if exists and path.is_file() else None


def _safe_postgres_schema_label(dsn: str, schema: str) -> str:
    parsed = urlparse(dsn)
    host = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    database = parsed.path.lstrip("/") or "postgres"
    return f"{parsed.scheme}://{host}{port}/{database}#{schema}"


def _csv_row_count(path: Path) -> int | None:
    if not path.exists() or not path.is_file():
        return None
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            next(reader)
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def _execute_many(connection: Any, sql: str, rows: Iterable[tuple[Any, ...]], batch_size: int) -> int:
    total = 0
    batch: list[tuple[Any, ...]] = []
    with connection.cursor() as cursor:
        for row in rows:
            batch.append(row)
            if len(batch) >= batch_size:
                cursor.executemany(sql, batch)
                total += len(batch)
                batch.clear()
        if batch:
            cursor.executemany(sql, batch)
            total += len(batch)
    return total


def _table_exists(connection: Any, schema: str, table: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    ).fetchone()
    return row is not None


def _record_source_file(
    connection: Any,
    *,
    logical_name: str,
    path: Path | str,
    storage_kind: str = "file",
    row_count: int | None = None,
    notes: str | None = None,
    status_override: str | None = None,
) -> int:
    path_text, exists, byte_size = _path_metadata(path)
    status = status_override or ("ready" if exists else "missing")
    row = connection.execute(
        """
        INSERT INTO governance.source_files (
          logical_name, path, storage_kind, status, row_count, byte_size, sha256, notes, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (logical_name) DO UPDATE SET
          path = excluded.path,
          storage_kind = excluded.storage_kind,
          status = excluded.status,
          row_count = excluded.row_count,
          byte_size = excluded.byte_size,
          sha256 = excluded.sha256,
          notes = excluded.notes,
          updated_at = now()
        RETURNING source_file_id
        """,
        (logical_name, path_text, storage_kind, status, row_count, byte_size, _sha256(path), notes),
    ).fetchone()
    return int(row["source_file_id"])


def _start_batch(connection: Any, dataset_key: str, source_file_id: int | None = None) -> int:
    row = connection.execute(
        """
        INSERT INTO governance.import_batches (dataset_key, source_file_id)
        VALUES (%s, %s)
        RETURNING import_batch_id
        """,
        (dataset_key, source_file_id),
    ).fetchone()
    return int(row["import_batch_id"])


def _finish_batch(connection: Any, import_batch_id: int, row_count: int, status: str = "completed", error: str | None = None) -> None:
    connection.execute(
        """
        UPDATE governance.import_batches
        SET finished_at = now(), status = %s, row_count = %s, error_message = %s
        WHERE import_batch_id = %s
        """,
        (status, row_count, error, import_batch_id),
    )


def truncate_governed_tables(connection: Any) -> None:
    connection.execute(
        """
        TRUNCATE
          core.polymer_properties,
          core.polymers,
          knowledge.formulation_records,
          knowledge.documents,
          online_knowledge.jobs,
          online_knowledge.history,
          pi.tg_predictions,
          pi.polymers,
          pi.monomer_iupac,
          dft.energy_trace,
          dft.molecule_final,
          lab.sample_measurements,
          lab.test_projects,
          experimental.process_records,
          experimental.property_records,
          model_registry.assets
        RESTART IDENTITY CASCADE
        """
    )


def upsert_source_registry(connection: Any, settings: Settings, target_dsn: str | None = None) -> dict[str, int]:
    postgres_label = _safe_postgres_schema_label(target_dsn or settings.app_postgres_dsn, "data_collection_demo")
    sources: list[tuple[str, Path | str, int | None, str, str | None, str]] = [
        ("core_property_csv", settings.csv_source_file, _csv_row_count(settings.csv_source_file), "authoritative property CSV", None, "file"),
        (
            "property_filter_csv",
            settings.property_filter_csv_file,
            _csv_row_count(settings.property_filter_csv_file),
            "standardized high-confidence property-filter CSV",
            None,
            "file",
        ),
        ("knowledge_zip", settings.knowledge_zip_file, None, "local knowledge archive", None, "file"),
        (
            "main_sqlite",
            settings.legacy_main_sqlite_source_file,
            None,
            "archived legacy SQLite source; retained for audit/import rollback only",
            "archived_legacy_runtime_source",
            "file",
        ),
        (
            "pi_sqlite",
            settings.legacy_pi_sqlite_source_file,
            None,
            "archived legacy PI reverse-design SQLite source; retained for audit/import rollback only",
            "archived_legacy_runtime_source",
            "file",
        ),
        (
            "dft_sqlite",
            settings.legacy_dft_sqlite_source_file,
            None,
            "archived legacy DFT SQLite source; retained for audit/import rollback only",
            "archived_legacy_runtime_source",
            "symlink" if settings.legacy_dft_sqlite_source_file.is_symlink() else "file",
        ),
        (
            "experimental_process_csv",
            settings.experimental_process_csv_file,
            _csv_row_count(settings.experimental_process_csv_file),
            "optional experimental process CSV",
            None,
            "file",
        ),
        (
            "experimental_property_csv",
            settings.experimental_property_csv_file,
            _csv_row_count(settings.experimental_property_csv_file),
            "optional experimental property CSV",
            None,
            "file",
        ),
        (
            "lab_legacy_demo_postgres",
            postgres_label,
            None,
            "legacy data_collection_demo schema imported into lab.*; retained as lineage marker only",
            "archived_legacy_runtime_source",
            "postgres-schema",
        ),
    ]
    source_ids: dict[str, int] = {}
    for logical_name, path, row_count, notes, status_override, storage_kind in sources:
        source_ids[logical_name] = _record_source_file(
            connection,
            logical_name=logical_name,
            path=path,
            storage_kind=storage_kind,
            row_count=row_count,
            notes=notes,
            status_override=status_override,
        )
    return source_ids


def import_source_registry(connection: Any, settings: Settings, target_dsn: str | None = None) -> DatasetImportStats:
    source_ids = upsert_source_registry(connection, settings, target_dsn)
    return DatasetImportStats(dataset_key="governance.source_files", row_count=len(source_ids))


IMPORT_BATCH_SOURCE_LOGICAL_NAMES = {
    "core": "core_property_csv",
    "knowledge": "main_sqlite",
    "online_knowledge": "main_sqlite",
    "pi": "pi_sqlite",
    "dft": "dft_sqlite",
    "lab": "lab_legacy_demo_postgres",
    "experimental_process": "experimental_process_csv",
    "experimental_property": "experimental_property_csv",
    "property_filter": "property_filter_csv",
}

FULL_IMPORT_DATASETS = {"sources", "assets", "core", "knowledge", "online", "pi", "dft", "experimental", "lab", "batch_backfill"}
GOVERNANCE_IMPORT_DATASETS = {"sources", "assets", "batch_backfill"}


def resolve_requested_datasets(datasets: set[str] | None) -> set[str]:
    requested = set(datasets or {"all"})
    if "all" in requested:
        requested = set(FULL_IMPORT_DATASETS)
    if "governance" in requested:
        requested.discard("governance")
        requested.update(GOVERNANCE_IMPORT_DATASETS)
    return requested


def backfill_import_batch_sources(connection: Any, source_ids: dict[str, int]) -> int:
    updated = 0
    for dataset_key, logical_name in IMPORT_BATCH_SOURCE_LOGICAL_NAMES.items():
        source_id = source_ids.get(logical_name)
        if source_id is None:
            continue
        rows = connection.execute(
            """
            UPDATE governance.import_batches
            SET source_file_id = %s
            WHERE dataset_key = %s AND source_file_id IS NULL
            RETURNING import_batch_id
            """,
            (source_id, dataset_key),
        ).fetchall()
        updated += len(rows)
    return updated


def import_model_registry(connection: Any, settings: Settings) -> DatasetImportStats:
    rows: list[tuple[Any, ...]] = []
    for spec in iter_model_asset_specs(include_directories=True):
        path = PROJECT_ROOT / spec.path
        exists = path.exists()
        is_file = exists and path.is_file()
        byte_size = path.stat().st_size if is_file else None
        sha256 = _sha256(path) if is_file else None
        rows.append(
            (
                spec.resolved_logical_name,
                str(path),
                spec.asset_type,
                byte_size,
                sha256,
                "ready" if exists else "missing",
                spec.notes or ("registered from release model asset manifest" if is_file else "registered as filesystem asset; model files remain outside Postgres"),
            )
        )
    connection.execute(
        "DELETE FROM model_registry.assets WHERE logical_name <> ALL(%s)",
        ([row[0] for row in rows],),
    )
    sql = """
        INSERT INTO model_registry.assets (
          logical_name, path, asset_type, byte_size, sha256, status, notes, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (logical_name) DO UPDATE SET
          path = excluded.path,
          asset_type = excluded.asset_type,
          byte_size = excluded.byte_size,
          sha256 = excluded.sha256,
          status = excluded.status,
          notes = excluded.notes,
          updated_at = now()
    """
    count = _execute_many(connection, sql, rows, 500)
    return DatasetImportStats(dataset_key="model_registry.assets", row_count=count)


def import_core_from_csv(connection: Any, csv_path: Path, batch_size: int) -> DatasetImportStats:
    polymer_rows: list[tuple[Any, ...]] = []
    property_rows: list[tuple[Any, ...]] = []
    polymer_index: dict[str, int] = {}

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for source_row_number, row in enumerate(reader, start=2):
            smiles = (row.get("smiles") or "").strip()
            polymer_id = polymer_index.get(smiles)
            if polymer_id is None:
                polymer_id = len(polymer_index) + 1
                canonical_smiles, rdkit_parse_ok = canonicalize_smiles(smiles)
                polymer_index[smiles] = polymer_id
                polymer_rows.append(
                    (
                        polymer_id,
                        (row.get("polymer_name") or "").strip() or None,
                        smiles,
                        canonical_smiles,
                        bool(rdkit_parse_ok),
                    )
                )
            property_rows.append(
                (
                    len(property_rows) + 1,
                    polymer_id,
                    (row.get("property_category") or "").strip() or "Others",
                    (row.get("property_name") or "").strip(),
                    (row.get("property_value") or "").strip(),
                    parse_float_or_none((row.get("property_value") or "").strip()),
                    normalize_property_unit(row.get("property_unit") or ""),
                    ((row.get("label_source") or "").strip() or None),
                    source_row_number,
                )
            )

    _execute_many(
        connection,
        """
        INSERT INTO core.polymers (polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (polymer_id) DO UPDATE SET
          polymer_name = excluded.polymer_name,
          smiles = excluded.smiles,
          canonical_smiles = excluded.canonical_smiles,
          rdkit_parse_ok = excluded.rdkit_parse_ok
        """,
        polymer_rows,
        batch_size,
    )
    _execute_many(
        connection,
        """
        INSERT INTO core.polymer_properties (
          property_id, polymer_id, property_category, property_name, property_value,
          property_value_num, property_unit, label_source, source_row_number
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (property_id) DO UPDATE SET
          polymer_id = excluded.polymer_id,
          property_category = excluded.property_category,
          property_name = excluded.property_name,
          property_value = excluded.property_value,
          property_value_num = excluded.property_value_num,
          property_unit = excluded.property_unit,
          label_source = excluded.label_source,
          source_row_number = excluded.source_row_number
        """,
        property_rows,
        batch_size,
    )
    return DatasetImportStats(
        dataset_key="core",
        row_count=len(property_rows),
        details={"polymers": len(polymer_rows), "polymer_properties": len(property_rows)},
    )


def import_property_filter_from_csv(connection: Any, csv_path: Path, batch_size: int) -> DatasetImportStats:
    required_columns = {
        "polymer_name",
        "smiles",
        "property_category",
        "property_name",
        "property_value",
        "property_unit",
        "property_unit_raw",
        "property_unit_clean",
        "property_key",
        "property_label",
        "canonical_value",
        "canonical_unit",
        "unit_conversion_status",
        "value_origin",
        "label_source",
        "reliable_score",
        "soft_quality_flags",
        "duplicate_flag",
    }
    source_file = str(csv_path)

    def clean_text(row: dict[str, str], key: str) -> str | None:
        value = (row.get(key) or "").strip()
        return value or None

    def iter_rows():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing = sorted(required_columns - set(reader.fieldnames or []))
            if missing:
                raise ValueError(f"property_filter CSV is missing required columns: {', '.join(missing)}")
            for filter_record_id, row in enumerate(reader, start=1):
                source_row_number = filter_record_id + 1
                smiles = clean_text(row, "smiles")
                canonical_smiles, rdkit_parse_ok = canonicalize_smiles(smiles or "")
                yield (
                    filter_record_id,
                    source_file,
                    source_row_number,
                    clean_text(row, "polymer_name"),
                    smiles,
                    canonical_smiles,
                    bool(rdkit_parse_ok),
                    clean_text(row, "property_category") or "Others",
                    clean_text(row, "property_name") or "",
                    clean_text(row, "property_value") or "",
                    parse_float_or_none(clean_text(row, "property_value") or ""),
                    clean_text(row, "property_unit"),
                    clean_text(row, "property_unit_raw"),
                    clean_text(row, "property_unit_clean"),
                    clean_text(row, "property_key"),
                    clean_text(row, "property_label"),
                    parse_float_or_none(clean_text(row, "canonical_value") or ""),
                    clean_text(row, "canonical_unit"),
                    clean_text(row, "unit_conversion_status"),
                    clean_text(row, "value_origin"),
                    clean_text(row, "label_source"),
                    parse_float_or_none(clean_text(row, "reliable_score") or ""),
                    clean_text(row, "soft_quality_flags"),
                    clean_text(row, "duplicate_flag"),
                )

    connection.execute("TRUNCATE core.polymer_property_filter_records")
    row_count = _execute_many(
        connection,
        """
        INSERT INTO core.polymer_property_filter_records (
          filter_record_id, source_file, source_row_number, polymer_name, smiles,
          canonical_smiles, rdkit_parse_ok, property_category, property_name,
          property_value, property_value_num, property_unit, property_unit_raw,
          property_unit_clean, property_key, property_label, canonical_value,
          canonical_unit, unit_conversion_status, value_origin, label_source,
          reliable_score, soft_quality_flags, duplicate_flag
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (filter_record_id) DO UPDATE SET
          source_file = excluded.source_file,
          source_row_number = excluded.source_row_number,
          polymer_name = excluded.polymer_name,
          smiles = excluded.smiles,
          canonical_smiles = excluded.canonical_smiles,
          rdkit_parse_ok = excluded.rdkit_parse_ok,
          property_category = excluded.property_category,
          property_name = excluded.property_name,
          property_value = excluded.property_value,
          property_value_num = excluded.property_value_num,
          property_unit = excluded.property_unit,
          property_unit_raw = excluded.property_unit_raw,
          property_unit_clean = excluded.property_unit_clean,
          property_key = excluded.property_key,
          property_label = excluded.property_label,
          canonical_value = excluded.canonical_value,
          canonical_unit = excluded.canonical_unit,
          unit_conversion_status = excluded.unit_conversion_status,
          value_origin = excluded.value_origin,
          label_source = excluded.label_source,
          reliable_score = excluded.reliable_score,
          soft_quality_flags = excluded.soft_quality_flags,
          duplicate_flag = excluded.duplicate_flag
        """,
        iter_rows(),
        batch_size,
    )
    mapped_count = int(
        connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM core.polymer_property_filter_records
            WHERE property_key IS NOT NULL
            """
        ).fetchone()["count"]
    )
    return DatasetImportStats(
        dataset_key="property_filter",
        row_count=row_count,
        details={"mapped_records": mapped_count, "raw_records": row_count - mapped_count},
    )


def _clean_csv_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


EXPERIMENTAL_PROPERTY_CATEGORY_KEYWORDS = (
    ("Thermal", ("glass transition", "tg", "melting", "thermal", "decomposition", "heat", "enthalpy", "temperature")),
    ("Mechanical", ("tensile", "modulus", "strength", "elongation", "hardness", "impact", "flexural", "compressive", "stress", "strain", "young", "toughness")),
    ("Electrical", ("dielectric", "conductivity", "resistivity", "permittivity", "breakdown", "tracking", "voltage", "polarization", "loss tangent", "band gap", "bandgap")),
    ("Optical", ("refractive", "transmittance", "absorbance", "absorption", "wavelength", "photoluminescence", "fluorescence", "opacity", "haze", "uv", "color")),
    ("Barrier", ("permeability", "permeation", "oxygen", "carbon dioxide", "water vapor", "barrier")),
    ("Surface", ("contact angle", "surface", "roughness", "friction", "wettability")),
    ("Chemical", ("solubility", "viscosity", "density", "swelling", "water absorption", "moisture", "acid", "alkali", "chemical")),
)


def infer_experimental_property_category(property_name: str | None) -> str:
    normalized = (property_name or "").strip().lower()
    if not normalized:
        return "Others"
    for category, keywords in EXPERIMENTAL_PROPERTY_CATEGORY_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return category
    return "Others"


def import_experimental_process_from_csv(connection: Any, csv_path: Path, batch_size: int) -> DatasetImportStats:
    connection.execute("DELETE FROM experimental.process_records")
    if not csv_path.exists():
        return DatasetImportStats(dataset_key="experimental_process", row_count=0)

    def rows():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for source_row_number, row in enumerate(reader, start=2):
                yield (
                    csv_path.name,
                    source_row_number,
                    _clean_csv_value(row.get("polymer_id")),
                    _clean_csv_value(row.get("polymer_name")),
                    _clean_csv_value(row.get("product_name")),
                    _clean_csv_value(row.get("process_flow_original_text")),
                    _clean_csv_value(row.get("material_original_text")),
                    Jsonb(dict(row)),
                )

    count = _execute_many(
        connection,
        """
        INSERT INTO experimental.process_records (
          source_file, source_row_number, polymer_id, polymer_name, product_name,
          process_flow_original_text, material_original_text, raw_data
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_file, source_row_number) DO UPDATE SET
          polymer_id = excluded.polymer_id,
          polymer_name = excluded.polymer_name,
          product_name = excluded.product_name,
          process_flow_original_text = excluded.process_flow_original_text,
          material_original_text = excluded.material_original_text,
          raw_data = excluded.raw_data
        """,
        rows(),
        batch_size,
    )
    return DatasetImportStats(dataset_key="experimental_process", row_count=count)


def import_experimental_property_from_csv(connection: Any, csv_path: Path, batch_size: int) -> DatasetImportStats:
    connection.execute("DELETE FROM experimental.property_records")
    if not csv_path.exists():
        return DatasetImportStats(dataset_key="experimental_property", row_count=0)

    def rows():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for source_row_number, row in enumerate(reader, start=2):
                property_name = _clean_csv_value(row.get("property_name_en"))
                property_category = _clean_csv_value(row.get("property_category") or row.get("dimension_class"))
                yield (
                    csv_path.name,
                    source_row_number,
                    _clean_csv_value(row.get("polymer_id")),
                    _clean_csv_value(row.get("polymer_name")),
                    property_category or infer_experimental_property_category(property_name),
                    property_name,
                    _clean_csv_value(row.get("value")),
                    Jsonb(dict(row)),
                )

    count = _execute_many(
        connection,
        """
        INSERT INTO experimental.property_records (
          source_file, source_row_number, polymer_id, polymer_name,
          property_category, property_name_en, value, raw_data
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_file, source_row_number) DO UPDATE SET
          polymer_id = excluded.polymer_id,
          polymer_name = excluded.polymer_name,
          property_category = excluded.property_category,
          property_name_en = excluded.property_name_en,
          value = excluded.value,
          raw_data = excluded.raw_data
        """,
        rows(),
        batch_size,
    )
    return DatasetImportStats(dataset_key="experimental_property", row_count=count)


def import_knowledge_from_sqlite(connection: Any, sqlite_db_path: Path, batch_size: int) -> DatasetImportStats:
    rows = _sqlite_rows(sqlite_db_path, "knowledge_documents")
    doc_rows = [
        (
            row["knowledge_id"],
            row["source_file"],
            row["source_row_number"],
            row["source_sequence"],
            row["title_zh"],
            row["title_en"],
            row["abstract"] or "",
            row["claim"],
            row["analysis"],
            row["is_polymer_synthesis"],
            row["judgement_reason"],
            row["polymer_iupac"],
            row["formulation"],
            row["catalyst"],
            row["temperature"],
            row["reaction_time"],
            row["solvent"],
            row["created_at"],
        )
        for row in rows
    ]
    _execute_many(
        connection,
        """
        INSERT INTO knowledge.documents (
          knowledge_id, source_file, source_row_number, source_sequence, title_zh, title_en,
          abstract, claim, analysis, is_polymer_synthesis, judgement_reason, polymer_iupac,
          formulation, catalyst, temperature, reaction_time, solvent, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()))
        ON CONFLICT (knowledge_id) DO UPDATE SET
          source_file = excluded.source_file,
          source_row_number = excluded.source_row_number,
          source_sequence = excluded.source_sequence,
          title_zh = excluded.title_zh,
          title_en = excluded.title_en,
          abstract = excluded.abstract,
          claim = excluded.claim,
          analysis = excluded.analysis,
          is_polymer_synthesis = excluded.is_polymer_synthesis,
          judgement_reason = excluded.judgement_reason,
          polymer_iupac = excluded.polymer_iupac,
          formulation = excluded.formulation,
          catalyst = excluded.catalyst,
          temperature = excluded.temperature,
          reaction_time = excluded.reaction_time,
          solvent = excluded.solvent,
          created_at = excluded.created_at
        """,
        doc_rows,
        batch_size,
    )
    connection.execute("DELETE FROM knowledge.formulation_records")
    connection.execute(
        """
        INSERT INTO knowledge.formulation_records (
          knowledge_id, source_file, source_row_number, polymer_iupac, formulation,
          catalyst, temperature, reaction_time, solvent, created_at
        )
        SELECT
          knowledge_id, source_file, source_row_number, polymer_iupac, formulation,
          catalyst, temperature, reaction_time, solvent, created_at
        FROM knowledge.documents
        WHERE NULLIF(BTRIM(COALESCE(formulation, '')), '') IS NOT NULL
           OR NULLIF(BTRIM(COALESCE(catalyst, '')), '') IS NOT NULL
           OR NULLIF(BTRIM(COALESCE(temperature, '')), '') IS NOT NULL
           OR NULLIF(BTRIM(COALESCE(reaction_time, '')), '') IS NOT NULL
           OR NULLIF(BTRIM(COALESCE(solvent, '')), '') IS NOT NULL
        """
    )
    formulation_count = int(connection.execute("SELECT COUNT(*) AS count FROM knowledge.formulation_records").fetchone()["count"])
    return DatasetImportStats(
        dataset_key="knowledge",
        row_count=len(doc_rows),
        details={"documents": len(doc_rows), "formulation_records": formulation_count},
    )


def _json_value(value: str | None) -> Jsonb:
    if not value:
        return Jsonb({})
    try:
        return Jsonb(json.loads(value))
    except json.JSONDecodeError:
        return Jsonb({"raw": value})


def reset_online_history_sequence(connection: Any) -> None:
    connection.execute(
        """
        SELECT setval(
          'online_knowledge.history_history_id_seq',
          COALESCE((SELECT max(history_id) FROM online_knowledge.history), 1),
          EXISTS (SELECT 1 FROM online_knowledge.history)
        )
        """
    )


def import_online_knowledge_from_sqlite(connection: Any, sqlite_db_path: Path, batch_size: int) -> DatasetImportStats:
    history_rows = [
        (
            row["history_id"],
            row["material"],
            row["mode"],
            row["created_at"],
            row["papers_found"],
            row["reactions_extracted"],
            row["max_papers"],
            _json_value(row["result_json"]),
        )
        for row in _sqlite_rows(sqlite_db_path, "online_knowledge_history")
    ]
    job_rows = [
        (
            row["job_id"],
            row["status"],
            row["material"],
            row["mode"],
            row["max_papers"],
            row["progress_stage"],
            row["progress_message"],
            row["processed_papers"],
            row["total_papers"],
            row["created_at"],
            row["updated_at"],
            row["error_message"],
            _json_value(row["result_json"]),
        )
        for row in _sqlite_rows(sqlite_db_path, "online_knowledge_jobs")
    ]
    _execute_many(
        connection,
        """
        INSERT INTO online_knowledge.history (
          history_id, material, mode, created_at, papers_found, reactions_extracted, max_papers, result_data
        ) VALUES (%s, %s, %s, COALESCE(%s::timestamptz, now()), %s, %s, %s, %s)
        ON CONFLICT (history_id) DO UPDATE SET
          material = excluded.material,
          mode = excluded.mode,
          created_at = excluded.created_at,
          papers_found = excluded.papers_found,
          reactions_extracted = excluded.reactions_extracted,
          max_papers = excluded.max_papers,
          result_data = excluded.result_data
        """,
        history_rows,
        batch_size,
    )
    reset_online_history_sequence(connection)
    _execute_many(
        connection,
        """
        INSERT INTO online_knowledge.jobs (
          job_id, status, material, mode, max_papers, progress_stage, progress_message,
          processed_papers, total_papers, created_at, updated_at, error_message, result_data
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()), COALESCE(%s::timestamptz, now()), %s, %s)
        ON CONFLICT (job_id) DO UPDATE SET
          status = excluded.status,
          material = excluded.material,
          mode = excluded.mode,
          max_papers = excluded.max_papers,
          progress_stage = excluded.progress_stage,
          progress_message = excluded.progress_message,
          processed_papers = excluded.processed_papers,
          total_papers = excluded.total_papers,
          created_at = excluded.created_at,
          updated_at = excluded.updated_at,
          error_message = excluded.error_message,
          result_data = excluded.result_data
        """,
        job_rows,
        batch_size,
    )
    return DatasetImportStats(
        dataset_key="online_knowledge",
        row_count=len(history_rows) + len(job_rows),
        details={"history": len(history_rows), "jobs": len(job_rows)},
    )


def import_pi_from_sqlite(connection: Any, sqlite_db_path: Path, batch_size: int) -> DatasetImportStats:
    rows = _sqlite_rows(sqlite_db_path, "pi_candidates")
    polymer_rows = [
        (
            row["pi_id"],
            row["mon1"],
            row["mon2"],
            row["polym"],
            row["canonical_polym"],
            bool(row["rdkit_parse_ok"]),
            row["morgan_fp"],
            row["created_at"],
        )
        for row in rows
    ]
    tg_rows = [
        (
            row["pi_id"],
            row["tg_celsius"],
            bool(row["rdkit_parse_ok"]),
            row["dielectric_const_dc"],
            row["static_dielectric_const"],
            row["dipole_debye"],
            row["electrophilicity_index"],
            row["homo_lumo_gap_ev"],
            row["hardness"],
            row["mulliken_electronegativity"],
            row["redox_window_v"],
            row["linear_expansion"],
            row["refractive_index"],
            row["created_at"],
        )
        for row in rows
    ]
    cache_rows = [
        (row["smiles"], row["iupac_name"], row["created_at"])
        for row in _sqlite_rows(sqlite_db_path, "smiles_iupac_cache")
    ]
    _execute_many(
        connection,
        """
        INSERT INTO pi.polymers (id, mon1, mon2, polym, canonical_polym, smiles_valid, morgan_fp, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()))
        ON CONFLICT (id) DO UPDATE SET
          mon1 = excluded.mon1,
          mon2 = excluded.mon2,
          polym = excluded.polym,
          canonical_polym = excluded.canonical_polym,
          smiles_valid = excluded.smiles_valid,
          morgan_fp = excluded.morgan_fp,
          created_at = excluded.created_at
        """,
        polymer_rows,
        batch_size,
    )
    _execute_many(
        connection,
        """
        INSERT INTO pi.tg_predictions (
          id, tg_celsius, smiles_valid, dielectric_const_dc, static_dielectric_const,
          dipole_debye, electrophilicity_index, homo_lumo_gap_ev, hardness,
          mulliken_electronegativity, redox_window_v, linear_expansion, refractive_index, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()))
        ON CONFLICT (id) DO UPDATE SET
          tg_celsius = excluded.tg_celsius,
          smiles_valid = excluded.smiles_valid,
          dielectric_const_dc = excluded.dielectric_const_dc,
          static_dielectric_const = excluded.static_dielectric_const,
          dipole_debye = excluded.dipole_debye,
          electrophilicity_index = excluded.electrophilicity_index,
          homo_lumo_gap_ev = excluded.homo_lumo_gap_ev,
          hardness = excluded.hardness,
          mulliken_electronegativity = excluded.mulliken_electronegativity,
          redox_window_v = excluded.redox_window_v,
          linear_expansion = excluded.linear_expansion,
          refractive_index = excluded.refractive_index,
          created_at = excluded.created_at
        """,
        tg_rows,
        batch_size,
    )
    _execute_many(
        connection,
        """
        INSERT INTO pi.monomer_iupac (smiles, iupac_name, created_at)
        VALUES (%s, %s, COALESCE(%s::timestamptz, now()))
        ON CONFLICT (smiles) DO UPDATE SET
          iupac_name = excluded.iupac_name,
          created_at = excluded.created_at
        """,
        cache_rows,
        batch_size,
    )
    return DatasetImportStats(
        dataset_key="pi",
        row_count=len(rows) + len(cache_rows),
        details={"polymers": len(polymer_rows), "tg_predictions": len(tg_rows), "monomer_iupac": len(cache_rows)},
    )


def import_dft_from_sqlite(connection: Any, sqlite_db_path: Path, batch_size: int) -> DatasetImportStats:
    molecule_count = _execute_many(
        connection,
        """
        INSERT INTO dft.molecule_final (
          mol_id, range_group, final_step, n_atoms, coordinates, scf_energy, zero_point_energy,
          thermal_enthalpy, gibbs_free_energy, lowest_freq, dipole_moment, homo_ev, lumo_ev,
          gap_ev, is_converged, pca_x, pca_y, pca_z
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (mol_id) DO UPDATE SET
          range_group = excluded.range_group,
          final_step = excluded.final_step,
          n_atoms = excluded.n_atoms,
          coordinates = excluded.coordinates,
          scf_energy = excluded.scf_energy,
          zero_point_energy = excluded.zero_point_energy,
          thermal_enthalpy = excluded.thermal_enthalpy,
          gibbs_free_energy = excluded.gibbs_free_energy,
          lowest_freq = excluded.lowest_freq,
          dipole_moment = excluded.dipole_moment,
          homo_ev = excluded.homo_ev,
          lumo_ev = excluded.lumo_ev,
          gap_ev = excluded.gap_ev,
          is_converged = excluded.is_converged,
          pca_x = excluded.pca_x,
          pca_y = excluded.pca_y,
          pca_z = excluded.pca_z
        """,
        (tuple(row[key] for key in row.keys()) for row in _iter_sqlite_rows(sqlite_db_path, "dft_molecule_final")),
        batch_size,
    )
    trace_count = _execute_many(
        connection,
        """
        INSERT INTO dft.energy_trace (mol_id, step, scf_energy, homo_ev, lumo_ev, gap_ev)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (mol_id, step) DO UPDATE SET
          scf_energy = excluded.scf_energy,
          homo_ev = excluded.homo_ev,
          lumo_ev = excluded.lumo_ev,
          gap_ev = excluded.gap_ev
        """,
        (tuple(row[key] for key in row.keys()) for row in _iter_sqlite_rows(sqlite_db_path, "dft_energy_trace")),
        batch_size,
    )
    return DatasetImportStats(
        dataset_key="dft",
        row_count=molecule_count + trace_count,
        details={"molecule_final": molecule_count, "energy_trace": trace_count},
    )


def import_lab_from_legacy_schema(connection: Any) -> DatasetImportStats:
    if not _table_exists(connection, "data_collection_demo", "test_projects"):
        return DatasetImportStats(dataset_key="lab", row_count=0)
    connection.execute(
        """
        INSERT INTO lab.test_projects (id, project_name, result_unit)
        SELECT id, project_name, result_unit FROM data_collection_demo.test_projects
        ON CONFLICT (id) DO UPDATE SET
          project_name = excluded.project_name,
          result_unit = excluded.result_unit
        """
    )
    project_count = int(connection.execute("SELECT COUNT(*) AS count FROM lab.test_projects").fetchone()["count"])
    measurement_count = 0
    if _table_exists(connection, "data_collection_demo", "sample_measurements"):
        connection.execute(
            """
            INSERT INTO lab.sample_measurements (
              id, sample_id, experiment_project, instrument_id, "operator", collection_time,
              temperature, concentration, result_value, result_unit, remarks
            )
            SELECT
              id, sample_id, experiment_project, instrument_id, "operator", collection_time,
              temperature, concentration, result_value, result_unit, remarks
            FROM data_collection_demo.sample_measurements
            ON CONFLICT (id) DO UPDATE SET
              sample_id = excluded.sample_id,
              experiment_project = excluded.experiment_project,
              instrument_id = excluded.instrument_id,
              "operator" = excluded."operator",
              collection_time = excluded.collection_time,
              temperature = excluded.temperature,
              concentration = excluded.concentration,
              result_value = excluded.result_value,
              result_unit = excluded.result_unit,
              remarks = excluded.remarks
            """
        )
        measurement_count = int(connection.execute("SELECT COUNT(*) AS count FROM lab.sample_measurements").fetchone()["count"])
    return DatasetImportStats(
        dataset_key="lab",
        row_count=project_count + measurement_count,
        details={"test_projects": project_count, "sample_measurements": measurement_count},
    )


def import_all_to_postgres(
    settings: Settings,
    *,
    dsn: str | None = None,
    datasets: set[str] | None = None,
    rebuild: bool = False,
    batch_size: int = 5000,
    apply_migrations: bool = True,
    refresh_analytics_snapshot: bool = False,
) -> PostgresImportStats:
    target_dsn = dsn or settings.app_postgres_dsn
    requested = resolve_requested_datasets(datasets)
    if rebuild and requested != FULL_IMPORT_DATASETS:
        selected = ", ".join(sorted(requested))
        raise ValueError(
            "--rebuild is only supported with a full import (`--dataset all` or no --dataset); "
            f"requested datasets: {selected}"
        )

    if apply_migrations:
        apply_postgres_migrations(target_dsn)

    stats = PostgresImportStats()
    source_ids: dict[str, int] = {}

    def ensure_source_ids(connection: Any) -> dict[str, int]:
        nonlocal source_ids
        if not source_ids:
            source_ids = upsert_source_registry(connection, settings, target_dsn)
        return source_ids

    with postgres_connection(target_dsn) as connection:
        with connection.transaction():
            if rebuild:
                truncate_governed_tables(connection)
            if requested & {"sources", "batch_backfill", "core", "knowledge", "online", "pi", "dft", "experimental", "lab", "property_filter"}:
                ensure_source_ids(connection)
            if "sources" in requested:
                stats.datasets.append(DatasetImportStats(dataset_key="governance.source_files", row_count=len(source_ids)))
            if "assets" in requested:
                stats.datasets.append(import_model_registry(connection, settings))
            if "core" in requested:
                if not settings.csv_source_file.exists():
                    raise FileNotFoundError(settings.csv_source_file)
                batch_id = _start_batch(connection, "core", ensure_source_ids(connection)["core_property_csv"])
                dataset_stats = import_core_from_csv(connection, settings.csv_source_file, batch_size)
                _finish_batch(connection, batch_id, dataset_stats.row_count)
                stats.datasets.append(dataset_stats)
            if "property_filter" in requested:
                if not settings.property_filter_csv_file.exists():
                    raise FileNotFoundError(settings.property_filter_csv_file)
                batch_id = _start_batch(connection, "property_filter", ensure_source_ids(connection)["property_filter_csv"])
                dataset_stats = import_property_filter_from_csv(connection, settings.property_filter_csv_file, batch_size)
                _finish_batch(connection, batch_id, dataset_stats.row_count)
                stats.datasets.append(dataset_stats)
            if "knowledge" in requested:
                batch_id = _start_batch(connection, "knowledge", ensure_source_ids(connection)["main_sqlite"])
                dataset_stats = import_knowledge_from_sqlite(connection, settings.legacy_main_sqlite_source_file, batch_size)
                _finish_batch(connection, batch_id, dataset_stats.row_count)
                stats.datasets.append(dataset_stats)
            if "online" in requested:
                batch_id = _start_batch(connection, "online_knowledge", ensure_source_ids(connection)["main_sqlite"])
                dataset_stats = import_online_knowledge_from_sqlite(connection, settings.legacy_main_sqlite_source_file, batch_size)
                _finish_batch(connection, batch_id, dataset_stats.row_count)
                stats.datasets.append(dataset_stats)
            if "pi" in requested:
                batch_id = _start_batch(connection, "pi", ensure_source_ids(connection)["pi_sqlite"])
                dataset_stats = import_pi_from_sqlite(connection, settings.legacy_pi_sqlite_source_file, batch_size)
                _finish_batch(connection, batch_id, dataset_stats.row_count)
                stats.datasets.append(dataset_stats)
            if "dft" in requested:
                batch_id = _start_batch(connection, "dft", ensure_source_ids(connection)["dft_sqlite"])
                dataset_stats = import_dft_from_sqlite(connection, settings.legacy_dft_sqlite_source_file, batch_size)
                _finish_batch(connection, batch_id, dataset_stats.row_count)
                stats.datasets.append(dataset_stats)
            if "experimental" in requested:
                batch_id = _start_batch(connection, "experimental_process", ensure_source_ids(connection)["experimental_process_csv"])
                dataset_stats = import_experimental_process_from_csv(connection, settings.experimental_process_csv_file, batch_size)
                _finish_batch(connection, batch_id, dataset_stats.row_count, "completed" if dataset_stats.row_count else "missing")
                stats.datasets.append(dataset_stats)

                batch_id = _start_batch(connection, "experimental_property", ensure_source_ids(connection)["experimental_property_csv"])
                dataset_stats = import_experimental_property_from_csv(connection, settings.experimental_property_csv_file, batch_size)
                _finish_batch(connection, batch_id, dataset_stats.row_count, "completed" if dataset_stats.row_count else "missing")
                stats.datasets.append(dataset_stats)
            if "lab" in requested:
                batch_id = _start_batch(connection, "lab", ensure_source_ids(connection)["lab_legacy_demo_postgres"])
                dataset_stats = import_lab_from_legacy_schema(connection)
                _finish_batch(connection, batch_id, dataset_stats.row_count)
                stats.datasets.append(dataset_stats)
            if "batch_backfill" in requested:
                updated_count = backfill_import_batch_sources(connection, ensure_source_ids(connection))
                stats.datasets.append(DatasetImportStats(dataset_key="governance.import_batches", row_count=updated_count))

    if refresh_analytics_snapshot:
        output_path = write_database_analytics_snapshot(target_dsn)
        stats.datasets.append(DatasetImportStats(dataset_key="database_analytics_snapshot", row_count=1, details={"bytes": output_path.stat().st_size}))
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Import PolyProp governed data into PostgreSQL.")
    parser.add_argument("--dsn", default=None, help="Target Postgres DSN. Defaults to APP_POSTGRES_DSN.")
    parser.add_argument("--dataset", action="append", choices=["all", "governance", "sources", "assets", "core", "knowledge", "online", "pi", "dft", "experimental", "lab", "property_filter"], help="Dataset to import. Repeatable. Defaults to all. governance updates source/model registries and backfills batch lineage only. property_filter imports the standardized threshold-filter CSV only when requested explicitly.")
    parser.add_argument("--refresh-analytics-snapshot", action="store_true", help="Regenerate the static database analytics snapshot after the import transaction commits.")
    parser.add_argument("--rebuild", action="store_true", help="Truncate governed target tables before importing.")
    parser.add_argument("--skip-migrations", action="store_true", help="Do not apply Postgres migrations before importing.")
    parser.add_argument("--batch-size", type=int, default=5000)
    args = parser.parse_args()

    settings = Settings()
    try:
        stats = import_all_to_postgres(
            settings,
            dsn=args.dsn,
            datasets=set(args.dataset or ["all"]),
            rebuild=args.rebuild,
            batch_size=max(1, args.batch_size),
            apply_migrations=not args.skip_migrations,
            refresh_analytics_snapshot=args.refresh_analytics_snapshot,
        )
    except ValueError as exc:
        parser.error(str(exc))
    for dataset in stats.datasets:
        details = " ".join(f"{key}={value}" for key, value in sorted(dataset.details.items()))
        suffix = f" {details}" if details else ""
        print(f"{dataset.dataset_key}\trows={dataset.row_count}{suffix}")


if __name__ == "__main__":
    main()
