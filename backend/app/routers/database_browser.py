from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from time import perf_counter

from fastapi import APIRouter, HTTPException, Query, Request

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
from app.services.database_analytics_snapshot import (
    STATIC_DATABASE_ANALYTICS_GENERATED_AT,
    get_database_analytics_snapshot,
)
from app.services.postgres_database_browser import (
    browse_dft_energy_steps_postgres,
    browse_dft_molecules_postgres,
    browse_experimental_process_records_postgres,
    browse_experimental_property_records_postgres,
    browse_formulation_records_postgres,
    browse_structure_property_records_postgres,
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

DATASET_TITLES = {
    "process": "Experimental Process Data",
    "property": "Experimental Property Data",
    "structureEffect": "Polymer Structure-Property Data",
    "propertyFilter": "Property Threshold Filter",
    "dft": "DFT Conformation Data",
    "formulation": "Formulation Ratio Data",
}
POSTGRES_ONLY_DETAIL = "Postgres runtime is required; set STRUCTURED_DATA_BACKEND=postgres."


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
    property_filter_status, property_filter_message = source_file_status(connection, "property_filter_csv")
    property_filter_total = 0
    if postgres_table_exists(connection, "core", "polymer_property_filter_records"):
        property_filter_total = int(connection.execute("SELECT COUNT(*) AS count FROM core.polymer_property_filter_records").fetchone()["count"])
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
        return DatabaseAnalyticsResponse(
            query_time_ms=(perf_counter() - started_at) * 1000,
            backend="postgres",
            source="snapshot",
            generated_at=STATIC_DATABASE_ANALYTICS_GENERATED_AT,
            datasets=get_database_analytics_snapshot(),
        )

    settings = request.app.state.settings
    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            return DatabaseAnalyticsResponse(
                query_time_ms=(perf_counter() - started_at) * 1000,
                backend="postgres",
                source="live",
                generated_at=None,
                datasets=get_database_analytics_postgres(connection),
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
def get_property_filter_options(request: Request) -> PropertyFilterOptionsResponse:
    started_at = perf_counter()
    settings = request.app.state.settings
    _require_postgres_browser(request)

    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if not postgres_table_exists(connection, "core", "polymer_property_filter_records"):
                raise RuntimeError("core.polymer_property_filter_records is missing")
            total_records, mapped_records, raw_records, rows = get_property_filter_options_postgres(connection)
            source_status, source_message = source_file_status(connection, "property_filter_csv")
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return PropertyFilterOptionsResponse(
        query_time_ms=(perf_counter() - started_at) * 1000,
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
def search_property_filter(request_body: PropertyFilterSearchRequest, request: Request) -> PropertyFilterSearchResponse:
    started_at = perf_counter()
    query = request_body.q.strip()
    settings = request.app.state.settings
    _require_postgres_browser(request)

    try:
        with request.app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            if not postgres_table_exists(connection, "core", "polymer_property_filter_records"):
                raise RuntimeError("core.polymer_property_filter_records is missing")
            total_records, matched_records, rows = search_property_filter_records_postgres(
                connection,
                conditions=request_body.filters,
                query=query,
                page=request_body.page,
                page_size=request_body.page_size,
            )
            source_status, source_message = source_file_status(connection, "property_filter_csv")
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

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

    return PropertyFilterSearchResponse(
        query=query,
        page=request_body.page,
        page_size=request_body.page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
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
