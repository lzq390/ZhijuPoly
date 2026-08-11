from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from time import perf_counter

from fastapi import APIRouter, HTTPException, Query, Request, Response
from psycopg.errors import QueryCanceled

from app.config import PROJECT_ROOT
from app.models import (
    DatabaseAnalyticsResponse,
    DatasetSummaryItem,
    DatasetSummaryResponse,
    DftEnergyStepBrowseResponse,
    DftEnergyStepRecord,
    DftMoleculeBrowserRecord,
    DftMoleculeBrowseResponse,
    ExperimentalProcessBrowseResponse,
    ExperimentalProcessRecord,
    ExperimentalPropertyBrowseResponse,
    ExperimentalPropertyRecord,
    FormulationBrowseResponse,
    FormulationRecord,
    PropertyFilterOption,
    PropertyFilterOptionsResponse,
    PropertyFilterRecord,
    PropertyFilterSearchRequest,
    PropertyFilterSearchResponse,
    PropertyFilterSearchResult,
    SmilesLookupRequest,
    SmilesLookupResponse,
    SmilesLookupResult,
    StructurePropertyBrowseResponse,
    StructurePropertyRecord,
)
from app.postgres_database import PostgresUnavailableError
from app.services.analytics_snapshot_store import load_analytics_snapshot, save_analytics_snapshot
from app.services.property_filter_catalog import (
    PropertyFilterCatalog,
    load_property_filter_catalog,
    property_filter_catalog_is_current,
)
from app.services.postgres_database_browser import (
    browse_dft_energy_steps_postgres,
    browse_dft_molecules_postgres,
    browse_experimental_process_records_postgres,
    browse_experimental_property_records_postgres,
    browse_formulation_records_postgres,
    browse_structure_property_records_postgres,
    database_analytics_sources_changed_postgres,
    get_property_filter_options_postgres,
    get_database_analytics_postgres,
    get_dft_browser_summary_postgres,
    lookup_pi_candidate_smiles_postgres,
    lookup_polymer_smiles_postgres,
    lookup_property_smiles_postgres,
    postgres_table_exists,
    search_property_filter_records_postgres,
    source_file_status,
)
from app.services.smiles_utils import normalize
from app.services.structure_2d import generate_2d_svg


router = APIRouter(prefix="/api/v1/database-browser", tags=["database-browser"])
logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)

DATASET_TITLES = {
    "process": "Experimental Process Data",
    "property": "Experimental Property Data",
    "structureEffect": "Polymer Structure-Property Data",
    "propertyFilter": "Property Threshold Filter",
    "dft": "DFT Conformation Data",
    "formulation": "Formulation Ratio Data",
}
POSTGRES_ONLY_DETAIL = "Postgres runtime is required; set STRUCTURED_DATA_BACKEND=postgres."


def _log_property_filter_event(
    event: str,
    *,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    logger.log(
        level,
        "%s",
        json.dumps(
            {"event": event, **fields},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def _require_postgres_browser(request: Request) -> None:
    if request.app.state.settings.structured_data_backend != "postgres":
        raise HTTPException(status_code=503, detail=POSTGRES_ONLY_DETAIL)


def _latest_import(connection, dataset_key: str) -> tuple[str | None, str | None]:
    if not postgres_table_exists(connection, "governance", "import_batches"):
        return None, None
    row = connection.execute(
        """
        SELECT status, finished_at
        FROM governance.import_batches
        WHERE dataset_key = %s
        ORDER BY started_at DESC, import_batch_id DESC
        LIMIT 1
        """,
        (dataset_key,),
    ).fetchone()
    if row is None:
        return None, None
    finished_at = row["finished_at"]
    return row["status"], finished_at.isoformat() if finished_at is not None else None


def _summary_item(
    *,
    key: str,
    total_records: int,
    data_source: str,
    source_status: str,
    source_message: str | None = None,
    latest_import_status: str | None = None,
    latest_import_finished_at: str | None = None,
) -> DatasetSummaryItem:
    return DatasetSummaryItem(
        key=key,
        title=DATASET_TITLES[key],
        total_records=total_records,
        data_source=data_source,
        source_status=source_status,
        source_message=source_message,
        latest_import_status=latest_import_status,
        latest_import_finished_at=latest_import_finished_at,
    )


def _source_file_label(path: Path) -> str:
    resolved_path = path.resolve()
    try:
        return resolved_path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved_path)


def _property_filter_source_status(connection, total_records: int) -> tuple[str, str | None]:
    source_status, source_message = source_file_status(connection, "property_filter_csv")
    if total_records == 0 and source_status == "ready":
        return "empty", "core.polymer_property_filter_records has no records; run the property_filter import."
    return source_status, source_message


def _current_property_filter_catalog(connection) -> PropertyFilterCatalog | None:
    if not postgres_table_exists(connection, "governance", "property_filter_options_snapshots"):
        return None
    try:
        catalog = load_property_filter_catalog(connection)
        if catalog is not None and property_filter_catalog_is_current(connection, catalog):
            return catalog
    except RuntimeError:
        logger.exception("property-filter catalog validation failed; using live fallback")
    return None


def _property_filter_etag(catalog: PropertyFilterCatalog, source_status: str) -> str:
    sha_prefix = (catalog.source_sha256 or "none")[:12]
    normalized_status = "".join(character for character in source_status if character.isalnum() or character in "-_")
    return f'W/"pf-options-v1-{catalog.generation}-{sha_prefix}-{normalized_status or "unknown"}"'


def _etag_matches(value: str | None, etag: str) -> bool:
    if value is None:
        return False
    return value.strip() == "*" or etag in {candidate.strip() for candidate in value.split(",")}


def _postgres_dataset_summaries(connection) -> list[DatasetSummaryItem]:
    items: list[DatasetSummaryItem] = []
    process_status, process_message = source_file_status(connection, "experimental_process_csv")
    process_total = 0
    if postgres_table_exists(connection, "experimental", "process_records"):
        process_total = int(connection.execute("SELECT COUNT(*) AS count FROM experimental.process_records").fetchone()["count"])
    property_status, property_message = source_file_status(connection, "experimental_property_csv")
    property_total = 0
    if postgres_table_exists(connection, "experimental", "property_records"):
        property_total = int(connection.execute("SELECT COUNT(*) AS count FROM experimental.property_records").fetchone()["count"])
    structure_total = int(connection.execute("SELECT COUNT(*) AS count FROM core.polymer_properties").fetchone()["count"])
    property_filter_total = 0
    if postgres_table_exists(connection, "core", "polymer_property_filter_records"):
        property_filter_catalog = _current_property_filter_catalog(connection)
        property_filter_total = (
            property_filter_catalog.total_records
            if property_filter_catalog is not None
            else int(connection.execute("SELECT COUNT(*) AS count FROM core.polymer_property_filter_records").fetchone()["count"])
        )
        property_filter_status, property_filter_message = _property_filter_source_status(connection, property_filter_total)
    else:
        property_filter_status = "missing"
        property_filter_message = "core.polymer_property_filter_records is missing."
    _, dft_total, _, _ = get_dft_browser_summary_postgres(connection)
    formulation_total = int(connection.execute("SELECT COUNT(*) AS count FROM knowledge.formulation_records").fetchone()["count"])
    for key, total, status, message, import_key in [
        ("process", process_total, process_status, process_message, "experimental_process"),
        ("property", property_total, property_status, property_message, "experimental_property"),
        ("structureEffect", structure_total, "ready", None, "core"),
        ("propertyFilter", property_filter_total, property_filter_status, property_filter_message, "property_filter"),
        ("dft", dft_total, "ready", None, "dft"),
        ("formulation", formulation_total, "ready", "Derived from knowledge.documents.", "knowledge"),
    ]:
        latest_status, latest_finished = _latest_import(connection, import_key)
        items.append(
            _summary_item(
                key=key,
                total_records=total,
                data_source="postgres",
                source_status=status,
                source_message=message,
                latest_import_status=latest_status,
                latest_import_finished_at=latest_finished,
            )
        )
    return items


def _normalize_query_smiles(smiles: str) -> str:
    try:
        return normalize(smiles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@lru_cache(maxsize=512)
def _lookup_structure_svg(smiles: str, canonical_smiles: str | None) -> str | None:
    return generate_2d_svg(canonical_smiles or smiles)


def _polymer_lookup_result(row) -> SmilesLookupResult:
    return SmilesLookupResult(
        record_id=str(row["polymer_id"]),
        source_column=row["source_column"],
        smiles=row["smiles"],
        canonical_smiles=row["canonical_smiles"],
        structure_svg=_lookup_structure_svg(row["smiles"], row["canonical_smiles"]),
        summary=f"polymer #{row['polymer_id']}",
        fields={
            "polymer_id": int(row["polymer_id"]),
            "rdkit_parse_ok": bool(row["rdkit_parse_ok"]),
            "property_count": int(row["property_count"]),
        },
    )


def _property_lookup_result(row) -> SmilesLookupResult:
    return SmilesLookupResult(
        record_id=str(row["property_id"]),
        source_column=row["source_column"],
        smiles=row["smiles"],
        canonical_smiles=row["canonical_smiles"],
        structure_svg=_lookup_structure_svg(row["smiles"], row["canonical_smiles"]),
        summary=row["property_name"],
        fields={
            "property_id": int(row["property_id"]),
            "polymer_id": int(row["polymer_id"]),
            "property_name": row["property_name"],
            "property_value": row["property_value"],
            "property_value_num": row["property_value_num"],
            "property_unit": row["property_unit"],
            "label_source": row["label_source"],
        },
    )


def _pi_candidate_lookup_result(row) -> SmilesLookupResult:
    return SmilesLookupResult(
        record_id=str(row["pi_id"]),
        source_column=row["source_column"],
        smiles=row["matched_smiles"],
        canonical_smiles=row["canonical_polym"],
        structure_svg=_lookup_structure_svg(row["matched_smiles"], row["canonical_polym"]),
        summary=f"PI candidate #{row['pi_id']}",
        fields={
            "pi_id": int(row["pi_id"]),
            "mon1": row["mon1"],
            "mon2": row["mon2"],
            "polym": row["polym"],
            "rdkit_parse_ok": bool(row["rdkit_parse_ok"]),
            "tg_celsius": row["tg_celsius"],
            "dielectric_const_dc": row["dielectric_const_dc"],
            "static_dielectric_const": row["static_dielectric_const"],
            "dipole_debye": row["dipole_debye"],
            "electrophilicity_index": row["electrophilicity_index"],
            "homo_lumo_gap_ev": row["homo_lumo_gap_ev"],
            "hardness": row["hardness"],
            "mulliken_electronegativity": row["mulliken_electronegativity"],
            "redox_window_v": row["redox_window_v"],
            "linear_expansion": row["linear_expansion"],
            "refractive_index": row["refractive_index"],
        },
    )


@router.get("/datasets/summary", response_model=DatasetSummaryResponse)
def get_dataset_summaries(request: Request) -> DatasetSummaryResponse:
    started_at = perf_counter()
    _require_postgres_browser(request)
    settings = request.app.state.settings
    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            required = [
                ("core", "polymers"),
                ("core", "polymer_properties"),
                ("knowledge", "documents"),
                ("knowledge", "formulation_records"),
                ("dft", "molecule_final"),
                ("dft", "energy_trace"),
                ("experimental", "process_records"),
                ("experimental", "property_records"),
                ("core", "polymer_property_filter_records"),
            ]
            missing = [f"{schema}.{table}" for schema, table in required if not postgres_table_exists(connection, schema, table)]
            if missing:
                raise HTTPException(status_code=503, detail=f"Postgres governed tables are missing: {', '.join(missing)}")
            return DatasetSummaryResponse(
                query_time_ms=(perf_counter() - started_at) * 1000,
                backend="postgres",
                datasets=_postgres_dataset_summaries(connection),
            )
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc


@router.get("/datasets/analytics", response_model=DatabaseAnalyticsResponse)
def get_dataset_analytics(request: Request, refresh: bool = Query(default=False)) -> DatabaseAnalyticsResponse:
    started_at = perf_counter()
    _require_postgres_browser(request)

    if not refresh:
        settings = request.app.state.settings
        try:
            with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
                stored_snapshot = load_analytics_snapshot(connection)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="Postgres analytics snapshot is unavailable",
            ) from exc
        if stored_snapshot is None:
            raise HTTPException(status_code=503, detail="Postgres analytics snapshot is missing")
        return DatabaseAnalyticsResponse(
            query_time_ms=(perf_counter() - started_at) * 1000,
            backend="postgres",
            source="snapshot",
            generated_at=stored_snapshot.generated_at.isoformat(),
            datasets=stored_snapshot.datasets,
        )

    settings = request.app.state.settings
    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            try:
                stored_snapshot = load_analytics_snapshot(connection)
            except RuntimeError:
                logger.exception("database analytics snapshot validation failed; rebuilding")
                stored_snapshot = None
            if stored_snapshot is not None and not database_analytics_sources_changed_postgres(
                connection,
                generated_at=stored_snapshot.generated_at,
                datasets=stored_snapshot.datasets,
            ):
                return DatabaseAnalyticsResponse(
                    query_time_ms=(perf_counter() - started_at) * 1000,
                    backend="postgres",
                    source="snapshot",
                    refresh_status="unchanged",
                    generated_at=stored_snapshot.generated_at.isoformat(),
                    datasets=stored_snapshot.datasets,
                )

            datasets = get_database_analytics_postgres(connection)
            refreshed_snapshot = save_analytics_snapshot(
                connection,
                datasets,
                source_sha=stored_snapshot.source_sha if stored_snapshot is not None else None,
            )
            return DatabaseAnalyticsResponse(
                query_time_ms=(perf_counter() - started_at) * 1000,
                backend="postgres",
                source="live",
                refresh_status="recomputed",
                generated_at=refreshed_snapshot.generated_at.isoformat(),
                datasets=refreshed_snapshot.datasets,
            )
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc


@router.post("/smiles-lookup", response_model=SmilesLookupResponse)
async def lookup_smiles(request_body: SmilesLookupRequest, request: Request) -> SmilesLookupResponse:
    started_at = perf_counter()
    query_smiles = request_body.smiles.strip()
    canonical_smiles = _normalize_query_smiles(query_smiles)
    settings = request.app.state.settings
    _require_postgres_browser(request)

    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if request_body.table == "polymers":
                total, rows = lookup_polymer_smiles_postgres(
                    connection,
                    query_smiles=query_smiles,
                    canonical_smiles=canonical_smiles,
                )
                results = [_polymer_lookup_result(row) for row in rows]
            elif request_body.table == "properties":
                total, rows = lookup_property_smiles_postgres(
                    connection,
                    query_smiles=query_smiles,
                    canonical_smiles=canonical_smiles,
                )
                results = [_property_lookup_result(row) for row in rows]
            else:
                total, rows = lookup_pi_candidate_smiles_postgres(
                    connection,
                    query_smiles=query_smiles,
                    canonical_smiles=canonical_smiles,
                )
                results = [_pi_candidate_lookup_result(row) for row in rows]
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc

    return SmilesLookupResponse(
        query_smiles=query_smiles,
        canonical_smiles=canonical_smiles,
        table=request_body.table,
        exists=total > 0,
        total=total,
        query_time_ms=(perf_counter() - started_at) * 1000,
        results=results,
    )


@router.get("/structure-property", response_model=StructurePropertyBrowseResponse)
async def browse_structure_property(
    request: Request,
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> StructurePropertyBrowseResponse:
    started_at = perf_counter()
    query = q.strip()
    settings = request.app.state.settings
    _require_postgres_browser(request)

    data_source = "postgres"
    source_status = "ready"
    source_message: str | None = None
    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if not postgres_table_exists(connection, "core", "polymer_properties"):
                raise RuntimeError("core.polymer_properties is missing")
            total_records, matched_records, rows = browse_structure_property_records_postgres(
                connection,
                query=query,
                page=page,
                page_size=page_size,
            )
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return StructurePropertyBrowseResponse(
        query=query,
        page=page,
        page_size=page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total_records=total_records,
        matched_records=matched_records,
        data_source=data_source,
        source_status=source_status,
        source_message=source_message,
        results=[
            StructurePropertyRecord(
                property_id=int(row["property_id"]),
                polymer_id=int(row["polymer_id"]),
                smiles=row["smiles"],
                canonical_smiles=row["canonical_smiles"],
                property_category=row["property_category"],
                property_name=row["property_name"],
                property_value=row["property_value"],
                property_value_num=row["property_value_num"],
                property_unit=row["property_unit"],
                label_source=row["label_source"],
            )
            for row in rows
        ],
    )


def _property_filter_record(row) -> PropertyFilterRecord:
    return PropertyFilterRecord(
        filter_record_id=int(row["filter_record_id"]),
        source_row_number=int(row["source_row_number"]),
        polymer_name=row["polymer_name"],
        smiles=row["smiles"],
        canonical_smiles=row["canonical_smiles"],
        property_category=row["property_category"],
        property_name=row["property_name"],
        property_value=row["property_value"],
        property_value_num=row["property_value_num"],
        property_unit_raw=row["property_unit_raw"],
        property_unit_clean=row["property_unit_clean"],
        property_key=row["property_key"],
        property_label=row["property_label"],
        canonical_value=row["canonical_value"],
        canonical_unit=row["canonical_unit"],
        unit_conversion_status=row["unit_conversion_status"],
        value_origin=row["value_origin"],
        label_source=row["label_source"],
        reliable_score=row["reliable_score"],
        soft_quality_flags=row["soft_quality_flags"],
        duplicate_flag=row["duplicate_flag"],
        filter_index=int(row["filter_index"]),
    )


@router.get("/property-filter/options", response_model=PropertyFilterOptionsResponse)
def get_property_filter_options(request: Request, response: Response):
    started_at = perf_counter()
    database_started_at = perf_counter()
    settings = request.app.state.settings
    _require_postgres_browser(request)

    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if not postgres_table_exists(connection, "core", "polymer_property_filter_records"):
                raise RuntimeError("core.polymer_property_filter_records is missing")
            catalog = _current_property_filter_catalog(connection)
            if catalog is None:
                total_records, mapped_records, raw_records, rows = get_property_filter_options_postgres(connection)
                mode = "live-fallback"
                _log_property_filter_event(
                    "property_filter_options_fallback",
                    level=logging.WARNING,
                    mode=mode,
                )
            else:
                total_records = catalog.total_records
                mapped_records = catalog.mapped_records
                raw_records = catalog.raw_records
                rows = catalog.options
                mode = "snapshot"
            source_status, source_message = _property_filter_source_status(connection, total_records)
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    database_duration_ms = (perf_counter() - database_started_at) * 1000
    duration_ms = (perf_counter() - started_at) * 1000
    headers = {
        "Cache-Control": "private, max-age=0, must-revalidate" if catalog is not None else "no-store",
        "Server-Timing": f"catalog;dur={duration_ms:.2f}, db;dur={database_duration_ms:.2f}",
    }
    if catalog is not None:
        etag = _property_filter_etag(catalog, source_status)
        headers["ETag"] = etag
        if _etag_matches(request.headers.get("if-none-match"), etag):
            _log_property_filter_event(
                "property_filter_options",
                mode="304",
                revision=etag[:48],
                database_duration_ms=round(database_duration_ms, 3),
                total_duration_ms=round(duration_ms, 3),
            )
            return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    _log_property_filter_event(
        "property_filter_options",
        mode=mode,
        revision=headers.get("ETag", "none")[:48],
        database_duration_ms=round(database_duration_ms, 3),
        total_duration_ms=round(duration_ms, 3),
        option_count=len(rows),
    )
    return PropertyFilterOptionsResponse(
        query_time_ms=duration_ms,
        total_records=total_records,
        mapped_records=mapped_records,
        raw_records=raw_records,
        data_source="postgres",
        source_status=source_status,
        source_message=source_message,
        options=[
            PropertyFilterOption(
                filter_type=row["filter_type"],
                option_key=row["option_key"],
                label=row["label"],
                property_key=row["property_key"],
                property_name=row["property_name"],
                property_unit_clean=row["property_unit_clean"],
                canonical_unit=row["canonical_unit"],
                rows=int(row["rows"]),
                unique_smiles=int(row["unique_smiles"]),
                min_value=row["min_value"],
                p5_value=row["p5_value"],
                median_value=row["median_value"],
                p95_value=row["p95_value"],
                max_value=row["max_value"],
            )
            for row in rows
        ],
    )


@router.post("/property-filter/search", response_model=PropertyFilterSearchResponse)
def search_property_filter(
    request_body: PropertyFilterSearchRequest,
    request: Request,
    response: Response,
) -> PropertyFilterSearchResponse:
    started_at = perf_counter()
    database_started_at = perf_counter()
    query = request_body.q.strip()
    settings = request.app.state.settings
    _require_postgres_browser(request)

    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if not postgres_table_exists(connection, "core", "polymer_property_filter_records"):
                raise RuntimeError("core.polymer_property_filter_records is missing")
            connection.execute("SET LOCAL statement_timeout = '20s'")
            total_records, matched_records, rows = search_property_filter_records_postgres(
                connection,
                conditions=request_body.filters,
                query=query,
                page=request_body.page,
                page_size=request_body.page_size,
            )
            source_status, source_message = _property_filter_source_status(connection, total_records)
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except QueryCanceled as exc:
        raise HTTPException(status_code=504, detail="Property filter query timed out") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    database_duration_ms = (perf_counter() - database_started_at) * 1000
    grouped: dict[str, list[PropertyFilterRecord]] = {}
    group_meta: dict[str, dict[str, str | None]] = {}
    for row in rows:
        group_key = row["group_key"]
        grouped.setdefault(group_key, []).append(_property_filter_record(row))
        group_meta.setdefault(
            group_key,
            {
                "smiles": row["smiles"],
                "canonical_smiles": row["canonical_smiles"],
                "polymer_name": row["polymer_name"],
            },
        )

    results = [
        PropertyFilterSearchResult(
            smiles=group_meta[group_key]["smiles"],
            canonical_smiles=group_meta[group_key]["canonical_smiles"],
            polymer_name=group_meta[group_key]["polymer_name"],
            matched_filters=len({record.filter_index for record in records}),
            records=records,
        )
        for group_key, records in grouped.items()
    ]

    duration_ms = (perf_counter() - started_at) * 1000
    response.headers["Cache-Control"] = "no-store"
    response.headers["Server-Timing"] = (
        f"search;dur={duration_ms:.2f}, db;dur={database_duration_ms:.2f}"
    )
    _log_property_filter_event(
        "property_filter_search",
        filter_count=len(request_body.filters),
        page=request_body.page,
        page_size=request_body.page_size,
        query_present=bool(query),
        matched_groups=matched_records,
        returned_groups=len(results),
        measurement_rows=len(rows),
        database_duration_ms=round(database_duration_ms, 3),
        total_duration_ms=round(duration_ms, 3),
    )
    return PropertyFilterSearchResponse(
        query=query,
        page=request_body.page,
        page_size=request_body.page_size,
        query_time_ms=duration_ms,
        total_records=total_records,
        matched_records=matched_records,
        data_source="postgres",
        source_status=source_status,
        source_message=source_message,
        results=results,
    )


@router.get("/dft/molecules", response_model=DftMoleculeBrowseResponse)
async def browse_dft_molecule_records(
    request: Request,
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> DftMoleculeBrowseResponse:
    started_at = perf_counter()
    query = q.strip()
    settings = request.app.state.settings
    _require_postgres_browser(request)

    data_source = "postgres"
    source_status = "ready"
    source_message: str | None = None
    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if not (postgres_table_exists(connection, "dft", "molecule_final") and postgres_table_exists(connection, "dft", "energy_trace")):
                raise RuntimeError("dft.molecule_final or dft.energy_trace is missing")
            total_records, matched_records, total_step_records, average_steps, max_steps, rows = browse_dft_molecules_postgres(
                connection,
                query=query,
                page=page,
                page_size=page_size,
            )
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return DftMoleculeBrowseResponse(
        query=query,
        page=page,
        page_size=page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total_records=total_records,
        matched_records=matched_records,
        total_step_records=total_step_records,
        average_steps=average_steps,
        max_steps=max_steps,
        data_source=data_source,
        source_status=source_status,
        source_message=source_message,
        results=[
            DftMoleculeBrowserRecord(
                mol_id=row["mol_id"],
                range_group=row["range_group"],
                final_step=int(row["final_step"]),
                n_atoms=int(row["n_atoms"]),
                trace_points=int(row["trace_points"]),
                scf_energy=row["scf_energy"],
                zero_point_energy=row["zero_point_energy"],
                thermal_enthalpy=row["thermal_enthalpy"],
                gibbs_free_energy=row["gibbs_free_energy"],
                lowest_freq=row["lowest_freq"],
                dipole_moment=row["dipole_moment"],
                homo_ev=row["homo_ev"],
                lumo_ev=row["lumo_ev"],
                gap_ev=row["gap_ev"],
                is_converged=row["is_converged"],
            )
            for row in rows
        ],
    )


@router.get("/dft/steps", response_model=DftEnergyStepBrowseResponse)
async def browse_dft_step_records(
    request: Request,
    q: str = Query(default="", max_length=200),
    mol_id: str | None = Query(default=None, max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> DftEnergyStepBrowseResponse:
    started_at = perf_counter()
    exact_mol_id = mol_id.strip() if mol_id is not None else ""
    query = exact_mol_id or q.strip()
    settings = request.app.state.settings
    _require_postgres_browser(request)

    data_source = "postgres"
    source_status = "ready"
    source_message: str | None = None
    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if not postgres_table_exists(connection, "dft", "energy_trace"):
                raise RuntimeError("dft.energy_trace is missing")
            total_records, matched_records, rows = browse_dft_energy_steps_postgres(
                connection,
                query=q.strip(),
                mol_id=exact_mol_id or None,
                page=page,
                page_size=page_size,
            )
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return DftEnergyStepBrowseResponse(
        query=query,
        page=page,
        page_size=page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total_records=total_records,
        matched_records=matched_records,
        data_source=data_source,
        source_status=source_status,
        source_message=source_message,
        results=[
            DftEnergyStepRecord(
                mol_id=row["mol_id"],
                step=int(row["step"]),
                scf_energy=row["scf_energy"],
                homo_ev=row["homo_ev"],
                lumo_ev=row["lumo_ev"],
                gap_ev=row["gap_ev"],
            )
            for row in rows
        ],
    )


@router.get("/formulation", response_model=FormulationBrowseResponse)
def browse_formulation_records(
    request: Request,
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> FormulationBrowseResponse:
    started_at = perf_counter()
    query = q.strip()
    settings = request.app.state.settings
    _require_postgres_browser(request)
    data_source = "postgres"
    source_status = "ready"
    source_message: str | None = None

    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if not postgres_table_exists(connection, "knowledge", "formulation_records"):
                raise RuntimeError("knowledge.formulation_records is missing")
            total_records, matched_records, rows = browse_formulation_records_postgres(
                connection,
                query=query,
                page=page,
                page_size=page_size,
            )
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return FormulationBrowseResponse(
        query=query,
        page=page,
        page_size=page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total_records=total_records,
        matched_records=matched_records,
        data_source=data_source,
        source_status=source_status,
        source_message=source_message,
        results=[
            FormulationRecord(
                formulation_id=int(row["formulation_id"]),
                knowledge_id=int(row["knowledge_id"]),
                source_file=row["source_file"],
                source_row_number=int(row["source_row_number"]),
                polymer_iupac=row["polymer_iupac"],
                formulation=row["formulation"],
                catalyst=row["catalyst"],
                temperature=row["temperature"],
                reaction_time=row["reaction_time"],
                solvent=row["solvent"],
            )
            for row in rows
        ],
    )


@router.get("/experimental-process", response_model=ExperimentalProcessBrowseResponse)
def browse_experimental_process_records(
    request: Request,
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> ExperimentalProcessBrowseResponse:
    started_at = perf_counter()
    query = q.strip()
    settings = request.app.state.settings
    _require_postgres_browser(request)

    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if not postgres_table_exists(connection, "experimental", "process_records"):
                raise RuntimeError("experimental.process_records is missing")
            total_records, matched_records, rows = browse_experimental_process_records_postgres(
                connection,
                query=query,
                page=page,
                page_size=page_size,
            )
            source_status, source_message = source_file_status(connection, "experimental_process_csv")
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return ExperimentalProcessBrowseResponse(
        query=query,
        page=page,
        page_size=page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total_records=total_records,
        matched_records=matched_records,
        data_source="postgres",
        source_status=source_status,
        source_message=source_message,
        results=[
            ExperimentalProcessRecord(
                source_file=row["source_file"],
                source_row_number=int(row["source_row_number"]),
                polymer_id=row["polymer_id"] or "",
                polymer_name=row["polymer_name"] or "",
                product_name=row["product_name"] or "",
                process_flow_original_text=row["process_flow_original_text"] or "",
                material_original_text=row["material_original_text"] or "",
            )
            for row in rows
        ],
    )


@router.get("/experimental-property", response_model=ExperimentalPropertyBrowseResponse)
def browse_experimental_property_records(
    request: Request,
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> ExperimentalPropertyBrowseResponse:
    started_at = perf_counter()
    query = q.strip()
    settings = request.app.state.settings
    _require_postgres_browser(request)

    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if not postgres_table_exists(connection, "experimental", "property_records"):
                raise RuntimeError("experimental.property_records is missing")
            total_records, matched_records, rows = browse_experimental_property_records_postgres(
                connection,
                query=query,
                page=page,
                page_size=page_size,
            )
            source_status, source_message = source_file_status(connection, "experimental_property_csv")
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return ExperimentalPropertyBrowseResponse(
        query=query,
        page=page,
        page_size=page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total_records=total_records,
        matched_records=matched_records,
        data_source="postgres",
        source_status=source_status,
        source_message=source_message,
        results=[
            ExperimentalPropertyRecord(
                source_file=row["source_file"],
                source_row_number=int(row["source_row_number"]),
                polymer_id=row["polymer_id"] or "",
                polymer_name=row["polymer_name"] or "",
                property_category=row["property_category"] or None,
                property_name_en=row["property_name_en"] or "",
                value=row["value"] or "",
            )
            for row in rows
        ],
    )
