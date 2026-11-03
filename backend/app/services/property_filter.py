"""Transport-independent, read-only property-filter queries.

Callers own the connection, transaction, statement timeout and access policy.
REST may use the historical live catalog fallback; external callers can require
a current catalog so their result references have a reproducible data revision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from app.models import (
    PropertyFilterHistogram,
    PropertyFilterOption,
    PropertyFilterRecord,
    PropertyFilterSearchRequest,
    PropertyFilterSearchResponse,
    PropertyFilterSearchResult,
)
from app.services.postgres_database_browser import (
    get_property_filter_options_postgres,
    postgres_table_exists,
    search_property_filter_records_postgres,
    source_file_status,
)
from app.services.property_filter_catalog import (
    PropertyFilterCatalog,
    load_property_filter_catalog,
    property_filter_catalog_is_current,
    resolve_property_filter_histogram,
)


logger = logging.getLogger(__name__)


class PropertyFilterCatalogUnavailable(RuntimeError):
    """No validated catalog describes the current imported dataset."""


class PropertyFilterOptionNotFound(ValueError):
    """The exact option key is absent from this catalog."""


@dataclass(frozen=True, slots=True)
class PropertyFilterContext:
    catalog: PropertyFilterCatalog | None
    options: list[dict[str, Any]]
    total_records: int
    mapped_records: int
    raw_records: int
    source_status: str
    source_message: str | None
    mode: str


@dataclass(frozen=True, slots=True)
class PropertyFilterGroupEvidence:
    group_key: str
    # Complete counts before sampling, keyed by the submitted condition index.
    # A measurement satisfying two conditions contributes to both counts.
    measurement_counts: dict[int, int]

    @property
    def total_measurement_count(self) -> int:
        return sum(self.measurement_counts.values())


@dataclass(frozen=True, slots=True)
class PropertyFilterSearchData:
    response: PropertyFilterSearchResponse
    # Same order as response.results, including when evidence is sampled.
    groups: list[PropertyFilterGroupEvidence]


def property_filter_source_status(connection: Any, total_records: int) -> tuple[str, str | None]:
    status, message = source_file_status(connection, "property_filter_csv")
    if total_records == 0 and status == "ready":
        return "empty", "core.polymer_property_filter_records has no records; run the property_filter import."
    return status, message


def current_property_filter_catalog(connection: Any) -> PropertyFilterCatalog | None:
    if not postgres_table_exists(connection, "governance", "property_filter_options_snapshots"):
        return None
    try:
        catalog = load_property_filter_catalog(connection)
        if catalog is not None and property_filter_catalog_is_current(connection, catalog):
            return catalog
    except RuntimeError:
        logger.exception("property-filter catalog validation failed; using live fallback when allowed")
    return None


def _require_property_filter_table(connection: Any) -> None:
    if not postgres_table_exists(connection, "core", "polymer_property_filter_records"):
        raise PropertyFilterCatalogUnavailable("core.polymer_property_filter_records is missing")


def load_property_filter_context(
    connection: Any,
    *,
    allow_live_fallback: bool = True,
) -> PropertyFilterContext:
    _require_property_filter_table(connection)
    catalog = current_property_filter_catalog(connection)
    if catalog is None:
        if not allow_live_fallback:
            raise PropertyFilterCatalogUnavailable("A current property-filter catalog is required")
        total_records, mapped_records, raw_records, options = get_property_filter_options_postgres(connection)
        mode = "live-fallback"
    else:
        total_records = catalog.total_records
        mapped_records = catalog.mapped_records
        raw_records = catalog.raw_records
        options = catalog.options
        mode = "snapshot"
    source_status, source_message = property_filter_source_status(connection, total_records)
    return PropertyFilterContext(
        catalog=catalog,
        options=options,
        total_records=total_records,
        mapped_records=mapped_records,
        raw_records=raw_records,
        source_status=source_status,
        source_message=source_message,
        mode=mode,
    )


def resolve_property_filter_option(context: PropertyFilterContext, option_key: str) -> PropertyFilterOption:
    # Keys are opaque. Do not parse, normalize or infer a unit from user input.
    option = next((row for row in context.options if row.get("option_key") == option_key), None)
    if option is None:
        raise PropertyFilterOptionNotFound("Property-filter option was not found")
    return PropertyFilterOption.model_validate(option)


def get_property_filter_histogram_data(
    connection: Any,
    context: PropertyFilterContext,
    option_key: str,
) -> tuple[PropertyFilterHistogram, str]:
    option = resolve_property_filter_option(context, option_key)
    histogram, mode = resolve_property_filter_histogram(connection, context.catalog, option.model_dump())
    return PropertyFilterHistogram.model_validate(histogram), mode


def property_filter_record(row: dict[str, Any]) -> PropertyFilterRecord:
    return PropertyFilterRecord.model_validate(
        {field: row[field] for field in PropertyFilterRecord.model_fields}
    )


def search_property_filter_data(
    connection: Any,
    request: PropertyFilterSearchRequest,
    *,
    measurement_limit_per_filter: int | None = None,
) -> PropertyFilterSearchData:
    started_at = perf_counter()
    _require_property_filter_table(connection)
    query = request.q.strip()
    total_records, matched_records, rows = search_property_filter_records_postgres(
        connection,
        conditions=request.filters,
        query=query,
        page=request.page,
        page_size=request.page_size,
        measurement_limit_per_filter=measurement_limit_per_filter,
    )
    source_status, source_message = property_filter_source_status(connection, total_records)
    grouped: dict[str, list[PropertyFilterRecord]] = {}
    counts: dict[str, dict[int, int]] = {}
    for row in rows:
        group_key = row["group_key"]
        record = property_filter_record(row)
        grouped.setdefault(group_key, []).append(record)
        group_counts = counts.setdefault(group_key, {})
        if measurement_limit_per_filter is None:
            group_counts[record.filter_index] = group_counts.get(record.filter_index, 0) + 1
        else:
            group_counts[record.filter_index] = int(row["matching_measurement_count"])

    results = [
        PropertyFilterSearchResult(
            smiles=records[0].smiles,
            canonical_smiles=records[0].canonical_smiles,
            polymer_name=records[0].polymer_name,
            matched_filters=len(counts[group_key]),
            records=records,
        )
        for group_key, records in grouped.items()
    ]
    return PropertyFilterSearchData(
        response=PropertyFilterSearchResponse(
            query=query,
            page=request.page,
            page_size=request.page_size,
            query_time_ms=(perf_counter() - started_at) * 1000,
            total_records=total_records,
            matched_records=matched_records,
            data_source="postgres",
            source_status=source_status,
            source_message=source_message,
            results=results,
        ),
        groups=[
            PropertyFilterGroupEvidence(group_key=group_key, measurement_counts=counts[group_key])
            for group_key in grouped
        ],
    )
