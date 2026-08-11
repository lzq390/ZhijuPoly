from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from threading import Lock
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.models import PropertyFilterOption


PROPERTY_FILTER_SNAPSHOT_KEY = "current"
# Histogram payloads are additive: schema-v1 snapshots without them stay valid and
# the selected option is computed on demand until the next atomic data import.
PROPERTY_FILTER_SNAPSHOT_SCHEMA_VERSION = 1
PROPERTY_FILTER_HISTOGRAM_BIN_COUNT = 18
PROPERTY_FILTER_HISTOGRAM_ROBUST_MIN_ROWS = 40
PROPERTY_FILTER_HISTOGRAM_CACHE_MAX_ENTRIES = 512


PropertyFilterHistogramCacheKey = tuple[int, datetime, int | None, str | None, str]
_property_filter_histogram_cache: OrderedDict[
    PropertyFilterHistogramCacheKey,
    dict[str, Any],
] = OrderedDict()
_property_filter_histogram_in_flight: dict[
    PropertyFilterHistogramCacheKey,
    Future[dict[str, Any]],
] = {}
_property_filter_histogram_cache_lock = Lock()


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


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if isfinite(normalized) else None


def _histogram_domain(option: dict[str, Any]) -> tuple[float, float, int, str] | None:
    minimum = _finite_float(option.get("min_value"))
    maximum = _finite_float(option.get("max_value"))
    if minimum is None or maximum is None or minimum > maximum:
        return None
    p5 = _finite_float(option.get("p5_value"))
    p95 = _finite_float(option.get("p95_value"))
    row_count = int(option.get("rows") or 0)
    if (
        row_count >= PROPERTY_FILTER_HISTOGRAM_ROBUST_MIN_ROWS
        and p5 is not None
        and p95 is not None
        and p5 < p95
    ):
        return p5, p95, PROPERTY_FILTER_HISTOGRAM_BIN_COUNT, "p5_p95"
    if minimum < maximum:
        return minimum, maximum, PROPERTY_FILTER_HISTOGRAM_BIN_COUNT, "full_range"
    return minimum, maximum, 1, "full_range"


def aggregate_property_filter_histograms(
    connection: Any,
    options: list[dict[str, Any]],
    *,
    schema: str | None = "core",
    table: str = "polymer_property_filter_records",
) -> dict[str, dict[str, Any]]:
    domains: list[tuple[Any, ...]] = []
    histograms: dict[str, dict[str, Any]] = {}
    for option in options:
        domain = _histogram_domain(option)
        option_key = str(option.get("option_key") or "")
        filter_type = option.get("filter_type")
        if not option_key or domain is None or filter_type not in {"standardized", "raw"}:
            continue
        domain_min, domain_max, bin_count, domain_kind = domain
        unit = (
            option.get("canonical_unit")
            if filter_type == "standardized"
            else option.get("property_unit_clean")
        )
        domains.append(
            (
                option_key,
                filter_type,
                option.get("property_key"),
                option.get("property_name"),
                unit or "",
                domain_min,
                domain_max,
                bin_count,
            )
        )
        histograms[option_key] = {
            "domain_min": domain_min,
            "domain_max": domain_max,
            "domain_kind": domain_kind,
            "bin_count": bin_count,
            "counts": [0] * bin_count,
            "underflow_count": 0,
            "overflow_count": 0,
            "total_count": 0,
        }
    if not domains:
        return histograms

    relation = _relation(schema, table)
    domain_rows = sql.SQL(", ").join(
        sql.SQL("(%s, %s, %s, %s, %s, %s, %s, %s)") for _ in domains
    )
    params = [value for domain in domains for value in domain]
    bucket_rows = connection.execute(
        sql.SQL(
            """
            WITH domains (
              option_key, filter_type, property_key, property_name, unit_value,
              domain_min, domain_max, bin_count
            ) AS (VALUES {domain_rows}),
            measurements AS (
              SELECT
                domains.option_key,
                domains.domain_min,
                domains.domain_max,
                domains.bin_count,
                records.canonical_value AS value
              FROM domains
              JOIN {relation} records
                ON domains.filter_type = 'standardized'
               AND records.property_key = domains.property_key
               AND COALESCE(records.canonical_unit, '') = domains.unit_value
              WHERE records.canonical_value IS NOT NULL

              UNION ALL

              SELECT
                domains.option_key,
                domains.domain_min,
                domains.domain_max,
                domains.bin_count,
                records.property_value_num AS value
              FROM domains
              JOIN {relation} records
                ON domains.filter_type = 'raw'
               AND records.property_key IS NULL
               AND records.property_name = domains.property_name
               AND COALESCE(records.property_unit_clean, '') = domains.unit_value
              WHERE records.property_value_num IS NOT NULL
            )
            SELECT
              option_key,
              CASE
                WHEN value < domain_min THEN 0
                WHEN value > domain_max THEN bin_count + 1
                WHEN domain_min = domain_max THEN 1
                ELSE LEAST(
                  bin_count,
                  FLOOR(((value - domain_min) / (domain_max - domain_min)) * bin_count)::integer + 1
                )
              END AS bucket,
              COUNT(*) AS count
            FROM measurements
            GROUP BY option_key, bucket
            ORDER BY option_key, bucket
            """
        ).format(domain_rows=domain_rows, relation=relation),
        params,
    ).fetchall()
    for row in bucket_rows:
        option_key = row["option_key"]
        histogram = histograms.get(option_key)
        if histogram is None:
            continue
        bucket = int(row["bucket"])
        count = int(row["count"] or 0)
        if bucket == 0:
            histogram["underflow_count"] = count
        elif bucket == histogram["bin_count"] + 1:
            histogram["overflow_count"] = count
        elif 1 <= bucket <= histogram["bin_count"]:
            histogram["counts"][bucket - 1] = count
        histogram["total_count"] += count
    return histograms


def _property_filter_histogram_cache_key(
    catalog: PropertyFilterCatalog,
    option_key: str,
) -> PropertyFilterHistogramCacheKey:
    return (
        catalog.generation,
        catalog.generated_at,
        catalog.import_batch_id,
        catalog.source_sha256,
        option_key,
    )


def resolve_property_filter_histogram(
    connection: Any,
    catalog: PropertyFilterCatalog | None,
    option: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Resolve one legacy histogram per catalog revision and backend process.

    Production intentionally runs one Uvicorn worker. A shared Future coalesces
    concurrent first views without mutating the schema-v1 snapshot that older
    rolling-release instances still need to validate. New imports embed every
    histogram and bypass this compatibility cache.
    """

    embedded = option.get("histogram")
    if embedded is not None:
        return embedded, "snapshot"

    option_key = str(option.get("option_key") or "")
    if not option_key:
        raise RuntimeError("Selected property has no option key")
    if catalog is None:
        histogram = aggregate_property_filter_histograms(connection, [option]).get(
            option_key
        )
        if histogram is None:
            raise RuntimeError("Selected property has no numeric histogram data")
        return histogram, "computed"
    cache_key = _property_filter_histogram_cache_key(catalog, option_key)
    owner = False
    with _property_filter_histogram_cache_lock:
        cached = _property_filter_histogram_cache.get(cache_key)
        if cached is not None:
            _property_filter_histogram_cache.move_to_end(cache_key)
            return cached, "cache-hit"
        pending = _property_filter_histogram_in_flight.get(cache_key)
        if pending is None:
            pending = Future()
            _property_filter_histogram_in_flight[cache_key] = pending
            owner = True

    if not owner:
        return pending.result(), "shared"

    try:
        histogram = aggregate_property_filter_histograms(connection, [option]).get(
            option_key
        )
        if histogram is None:
            raise RuntimeError("Selected property has no numeric histogram data")
    except BaseException as exc:
        with _property_filter_histogram_cache_lock:
            _property_filter_histogram_in_flight.pop(cache_key, None)
        pending.set_exception(exc)
        raise

    with _property_filter_histogram_cache_lock:
        _property_filter_histogram_cache[cache_key] = histogram
        _property_filter_histogram_cache.move_to_end(cache_key)
        while (
            len(_property_filter_histogram_cache)
            > PROPERTY_FILTER_HISTOGRAM_CACHE_MAX_ENTRIES
        ):
            _property_filter_histogram_cache.popitem(last=False)
        _property_filter_histogram_in_flight.pop(cache_key, None)
    pending.set_result(histogram)
    return histogram, "computed"


def reset_property_filter_histogram_cache_for_tests() -> None:
    with _property_filter_histogram_cache_lock:
        _property_filter_histogram_cache.clear()
        _property_filter_histogram_in_flight.clear()


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
    options = [dict(row) for row in rows]
    histograms = aggregate_property_filter_histograms(
        connection,
        options,
        schema=schema,
        table=table,
    )
    for option in options:
        option["histogram"] = histograms.get(option["option_key"])
    return (
        int(summary["total_records"] or 0),
        int(summary["mapped_records"] or 0),
        int(summary["raw_records"] or 0),
        options,
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
