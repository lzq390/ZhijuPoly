from __future__ import annotations

import csv
from pathlib import Path
from time import perf_counter

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import PROJECT_ROOT
from app.models import (
    DftEnergyStepBrowseResponse,
    DftEnergyStepRecord,
    DftMoleculeBrowserRecord,
    DftMoleculeBrowseResponse,
    ExperimentalProcessBrowseResponse,
    ExperimentalProcessRecord,
    ExperimentalPropertyBrowseResponse,
    ExperimentalPropertyRecord,
    SmilesLookupRequest,
    SmilesLookupResponse,
    SmilesLookupResult,
    StructurePropertyBrowseResponse,
    StructurePropertyRecord,
)
from app.services.database_browser import (
    browse_dft_energy_steps,
    browse_dft_molecules,
    browse_csv_records,
    browse_structure_property_records,
    lookup_pi_candidate_smiles,
    lookup_polymer_smiles,
    lookup_property_smiles,
)
from app.services.smiles_utils import normalize


router = APIRouter(prefix="/api/v1/database-browser", tags=["database-browser"])
EXPERIMENTAL_PROCESS_CSV = PROJECT_ROOT / "database/polymer_process_material_filtered_cleaned_office_utf8_bom.csv"
EXPERIMENTAL_PROPERTY_CSV = PROJECT_ROOT / "database/polymer_property_detail_cleaned_office_utf8_bom.csv"


def _source_file_label(path: Path) -> str:
    resolved_path = path.resolve()
    try:
        return resolved_path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved_path)


def _browse_csv_or_raise(
    csv_path: Path,
    *,
    query: str,
    page: int,
    page_size: int,
):
    if not csv_path.exists():
        raise HTTPException(status_code=503, detail=f"CSV source file not found: {_source_file_label(csv_path)}")

    try:
        return browse_csv_records(
            csv_path,
            source_file=_source_file_label(csv_path),
            query=query,
            page=page,
            page_size=page_size,
        )
    except (csv.Error, OSError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=503, detail=f"Failed to read CSV source: {exc}") from exc


def _normalize_query_smiles(smiles: str) -> str:
    try:
        return normalize(smiles)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _polymer_lookup_result(row) -> SmilesLookupResult:
    return SmilesLookupResult(
        record_id=str(row["polymer_id"]),
        source_column=row["source_column"],
        smiles=row["smiles"],
        canonical_smiles=row["canonical_smiles"],
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


@router.post("/smiles-lookup", response_model=SmilesLookupResponse)
async def lookup_smiles(request_body: SmilesLookupRequest, request: Request) -> SmilesLookupResponse:
    started_at = perf_counter()
    query_smiles = request_body.smiles.strip()
    canonical_smiles = _normalize_query_smiles(query_smiles)
    settings = request.app.state.settings

    if request_body.table == "polymers":
        with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
            total, rows = lookup_polymer_smiles(
                connection,
                query_smiles=query_smiles,
                canonical_smiles=canonical_smiles,
            )
        results = [_polymer_lookup_result(row) for row in rows]
    elif request_body.table == "properties":
        with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
            total, rows = lookup_property_smiles(
                connection,
                query_smiles=query_smiles,
                canonical_smiles=canonical_smiles,
            )
        results = [_property_lookup_result(row) for row in rows]
    else:
        if not settings.pi_reverse_db_file.exists():
            raise HTTPException(
                status_code=503,
                detail=f"PI reverse-design database not found: {_source_file_label(settings.pi_reverse_db_file)}",
            )

        with request.app.state.sqlite_connection_factory(settings.pi_reverse_db_file) as connection:
            total, rows = lookup_pi_candidate_smiles(
                connection,
                query_smiles=query_smiles,
                canonical_smiles=canonical_smiles,
            )
        results = [_pi_candidate_lookup_result(row) for row in rows]

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

    with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
        total_records, matched_records, rows = browse_structure_property_records(
            connection,
            query=query,
            page=page,
            page_size=page_size,
        )

    return StructurePropertyBrowseResponse(
        query=query,
        page=page,
        page_size=page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total_records=total_records,
        matched_records=matched_records,
        results=[
            StructurePropertyRecord(
                property_id=int(row["property_id"]),
                polymer_id=int(row["polymer_id"]),
                smiles=row["smiles"],
                canonical_smiles=row["canonical_smiles"],
                property_name=row["property_name"],
                property_value=row["property_value"],
                property_value_num=row["property_value_num"],
                property_unit=row["property_unit"],
                label_source=row["label_source"],
            )
            for row in rows
        ],
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

    with request.app.state.sqlite_connection_factory(settings.fumol_db_file) as connection:
        total_records, matched_records, total_step_records, average_steps, max_steps, rows = browse_dft_molecules(
            connection,
            query=query,
            page=page,
            page_size=page_size,
        )

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

    with request.app.state.sqlite_connection_factory(settings.fumol_db_file) as connection:
        total_records, matched_records, rows = browse_dft_energy_steps(
            connection,
            query=q.strip(),
            mol_id=exact_mol_id or None,
            page=page,
            page_size=page_size,
        )

    return DftEnergyStepBrowseResponse(
        query=query,
        page=page,
        page_size=page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total_records=total_records,
        matched_records=matched_records,
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


@router.get("/experimental-process", response_model=ExperimentalProcessBrowseResponse)
def browse_experimental_process_records(
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> ExperimentalProcessBrowseResponse:
    started_at = perf_counter()
    query = q.strip()
    if not EXPERIMENTAL_PROCESS_CSV.exists():
        return ExperimentalProcessBrowseResponse(
            query=query,
            page=page,
            page_size=page_size,
            query_time_ms=(perf_counter() - started_at) * 1000,
            total_records=0,
            matched_records=0,
            results=[],
        )

    total_records, matched_records, rows = _browse_csv_or_raise(
        EXPERIMENTAL_PROCESS_CSV,
        query=query,
        page=page,
        page_size=page_size,
    )

    return ExperimentalProcessBrowseResponse(
        query=query,
        page=page,
        page_size=page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total_records=total_records,
        matched_records=matched_records,
        results=[
            ExperimentalProcessRecord(
                source_file=row.source_file,
                source_row_number=row.source_row_number,
                polymer_id=row.data.get("polymer_id", ""),
                polymer_name=row.data.get("polymer_name", ""),
                product_name=row.data.get("product_name", ""),
                process_flow_original_text=row.data.get("process_flow_original_text", ""),
                material_original_text=row.data.get("material_original_text", ""),
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
    if not EXPERIMENTAL_PROPERTY_CSV.exists():
        settings = request.app.state.settings
        with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
            total_records, matched_records, rows = browse_structure_property_records(
                connection,
                query=query,
                page=page,
                page_size=page_size,
            )

        return ExperimentalPropertyBrowseResponse(
            query=query,
            page=page,
            page_size=page_size,
            query_time_ms=(perf_counter() - started_at) * 1000,
            total_records=total_records,
            matched_records=matched_records,
            results=[
                ExperimentalPropertyRecord(
                    source_file="sqlite:properties",
                    source_row_number=int(row["property_id"]),
                    polymer_id=str(row["polymer_id"]),
                    polymer_name="",
                    property_name_en=row["property_name"],
                    value=row["property_value"],
                )
                for row in rows
            ],
        )

    total_records, matched_records, rows = _browse_csv_or_raise(
        EXPERIMENTAL_PROPERTY_CSV,
        query=query,
        page=page,
        page_size=page_size,
    )

    return ExperimentalPropertyBrowseResponse(
        query=query,
        page=page,
        page_size=page_size,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total_records=total_records,
        matched_records=matched_records,
        results=[
            ExperimentalPropertyRecord(
                source_file=row.source_file,
                source_row_number=row.source_row_number,
                polymer_id=row.data.get("polymer_id", ""),
                polymer_name=row.data.get("polymer_name", ""),
                property_name_en=row.data.get("property_name_en", ""),
                value=row.data.get("value", ""),
            )
            for row in rows
        ],
    )
