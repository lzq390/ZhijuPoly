from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.models import PropertyFilterOption


PROPERTY_FILTER_SNAPSHOT_KEY = "current"
PROPERTY_FILTER_SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class PropertyFilterCatalog:
    schema_version: int
    generation: int
    import_batch_id: int | None
    source_sha256: str | None
    generated_at: datetime
    total_records: int
    mapped_records: int
    raw_records: int
    options: list[dict[str, Any]]


def _relation(schema: str | None, table: str) -> sql.Composed:
    if schema:
        return sql.SQL(".").join((sql.Identifier(schema), sql.Identifier(table)))
    return sql.Identifier(table)


def aggregate_property_filter_catalog(
    connection: Any,
    *,
    schema: str | None = "core",
    table: str = "polymer_property_filter_records",
) -> tuple[int, int, int, list[dict[str, Any]]]:
    relation = _relation(schema, table)
    summary = connection.execute(
        sql.SQL(
            """
            SELECT
              COUNT(*) AS total_records,
              COUNT(*) FILTER (WHERE property_key IS NOT NULL) AS mapped_records,
              COUNT(*) FILTER (WHERE property_key IS NULL) AS raw_records
            FROM {}
            """
        ).format(relation)
    ).fetchone()
    rows = connection.execute(
        sql.SQL(
            """
            WITH standardized AS (
              SELECT
                'standardized'::text AS filter_type,
                'std:' || property_key || ':' || COALESCE(canonical_unit, '') AS option_key,
                COALESCE(NULLIF(MIN(NULLIF(property_label, '')), ''), property_key) AS label,
                property_key,
                NULL::text AS property_name,
                NULL::text AS property_unit_clean,
                canonical_unit,
                COUNT(*) AS rows,
                COUNT(DISTINCT COALESCE(
                  NULLIF(canonical_smiles, ''),
                  NULLIF(smiles, ''),
                  'record:' || filter_record_id::text
                )) AS unique_smiles,
                MIN(canonical_value) AS min_value,
                percentile_cont(0.05) WITHIN GROUP (ORDER BY canonical_value) AS p5_value,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY canonical_value) AS median_value,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY canonical_value) AS p95_value,
                MAX(canonical_value) AS max_value
              FROM {relation}
              WHERE property_key IS NOT NULL
                AND canonical_value IS NOT NULL
              GROUP BY property_key, canonical_unit
            ),
            raw AS (
              SELECT
                'raw'::text AS filter_type,
                'raw:' || md5(property_name || '|' || COALESCE(property_unit_clean, '')) AS option_key,
                CASE
                  WHEN COALESCE(NULLIF(property_unit_clean, ''), '') = '' THEN property_name
                  ELSE property_name || ' (' || property_unit_clean || ')'
                END AS label,
                NULL::text AS property_key,
                property_name,
                property_unit_clean,
                NULL::text AS canonical_unit,
                COUNT(*) AS rows,
                COUNT(DISTINCT COALESCE(
                  NULLIF(canonical_smiles, ''),
                  NULLIF(smiles, ''),
                  'record:' || filter_record_id::text
                )) AS unique_smiles,
                MIN(property_value_num) AS min_value,
                percentile_cont(0.05) WITHIN GROUP (ORDER BY property_value_num) AS p5_value,
                percentile_cont(0.5) WITHIN GROUP (ORDER BY property_value_num) AS median_value,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY property_value_num) AS p95_value,
                MAX(property_value_num) AS max_value
              FROM {relation}
              WHERE property_key IS NULL
                AND property_value_num IS NOT NULL
              GROUP BY property_name, property_unit_clean
            )
            SELECT *
            FROM (
              SELECT * FROM standardized
              UNION ALL
              SELECT * FROM raw
            ) options
            ORDER BY
              CASE filter_type WHEN 'standardized' THEN 0 ELSE 1 END,
              rows DESC,
              label ASC
            """
        ).format(relation=relation)
    ).fetchall()
    return (
        int(summary["total_records"] or 0),
        int(summary["mapped_records"] or 0),
        int(summary["raw_records"] or 0),
        [dict(row) for row in rows],
    )


def save_property_filter_catalog(
    connection: Any,
    *,
    total_records: int,
    mapped_records: int,
    raw_records: int,
    options: list[dict[str, Any]],
    import_batch_id: int | None,
    source_sha256: str | None,
) -> PropertyFilterCatalog:
    row = connection.execute(
        """
        INSERT INTO governance.property_filter_options_snapshots (
          snapshot_key, schema_version, generation, import_batch_id,
          source_sha256, generated_at, total_records, mapped_records,
          raw_records, options, updated_at
        ) VALUES (%s, %s, 1, %s, %s, now(), %s, %s, %s, %s, now())
        ON CONFLICT (snapshot_key) DO UPDATE SET
          schema_version = excluded.schema_version,
          generation = governance.property_filter_options_snapshots.generation + 1,
          import_batch_id = excluded.import_batch_id,
          source_sha256 = excluded.source_sha256,
          generated_at = excluded.generated_at,
          total_records = excluded.total_records,
          mapped_records = excluded.mapped_records,
          raw_records = excluded.raw_records,
          options = excluded.options,
          updated_at = now()
        RETURNING
          schema_version, generation, import_batch_id, source_sha256,
          generated_at, total_records, mapped_records, raw_records, options
        """,
        (
            PROPERTY_FILTER_SNAPSHOT_KEY,
            PROPERTY_FILTER_SNAPSHOT_SCHEMA_VERSION,
            import_batch_id,
            source_sha256,
            total_records,
            mapped_records,
            raw_records,
            Jsonb(options),
        ),
    ).fetchone()
    return _catalog_from_row(row)


def rebuild_property_filter_catalog(
    connection: Any,
    *,
    import_batch_id: int | None = None,
    source_sha256: str | None = None,
    schema: str | None = "core",
    table: str = "polymer_property_filter_records",
) -> PropertyFilterCatalog:
    connection.execute("SET LOCAL work_mem = '128MB'")
    total_records, mapped_records, raw_records, options = aggregate_property_filter_catalog(
        connection,
        schema=schema,
        table=table,
    )
    return save_property_filter_catalog(
        connection,
        total_records=total_records,
        mapped_records=mapped_records,
        raw_records=raw_records,
        options=options,
        import_batch_id=import_batch_id,
        source_sha256=source_sha256,
    )


def load_property_filter_catalog(connection: Any) -> PropertyFilterCatalog | None:
    row = connection.execute(
        """
        SELECT
          schema_version, generation, import_batch_id, source_sha256,
          generated_at, total_records, mapped_records, raw_records, options
        FROM governance.property_filter_options_snapshots
        WHERE snapshot_key = %s
        """,
        (PROPERTY_FILTER_SNAPSHOT_KEY,),
    ).fetchone()
    return None if row is None else _catalog_from_row(row)


def property_filter_catalog_is_current(connection: Any, catalog: PropertyFilterCatalog) -> bool:
    if catalog.schema_version != PROPERTY_FILTER_SNAPSHOT_SCHEMA_VERSION:
        return False
    row = connection.execute(
        """
        SELECT MAX(import_batch_id) AS import_batch_id
        FROM governance.import_batches
        WHERE dataset_key = 'property_filter'
          AND status IN ('completed', 'empty')
        """
    ).fetchone()
    latest_import_batch_id = row["import_batch_id"]
    if latest_import_batch_id is None:
        return True
    if catalog.import_batch_id is None:
        return False
    return catalog.import_batch_id >= int(latest_import_batch_id)


def _catalog_from_row(row: Any) -> PropertyFilterCatalog:
    options = row["options"]
    if not isinstance(options, list) or any(not isinstance(option, dict) for option in options):
        raise RuntimeError("Stored property-filter options snapshot is not a JSON array of objects")
    try:
        for option in options:
            PropertyFilterOption.model_validate(option)
    except ValidationError as exc:
        raise RuntimeError("Stored property-filter options snapshot contains an invalid option") from exc
    total_records = int(row["total_records"] or 0)
    mapped_records = int(row["mapped_records"] or 0)
    raw_records = int(row["raw_records"] or 0)
    if min(total_records, mapped_records, raw_records) < 0 or mapped_records + raw_records != total_records:
        raise RuntimeError("Stored property-filter options snapshot has invalid record counts")
    return PropertyFilterCatalog(
        schema_version=int(row["schema_version"]),
        generation=int(row["generation"]),
        import_batch_id=int(row["import_batch_id"]) if row["import_batch_id"] is not None else None,
        source_sha256=row["source_sha256"],
        generated_at=row["generated_at"],
        total_records=total_records,
        mapped_records=mapped_records,
        raw_records=raw_records,
        options=options,
    )
