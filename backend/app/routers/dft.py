from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, HTTPException, Query, Request

from app.models import (
    DftEnergyPoint,
    DftMoleculeDetailResponse,
    DftPcaPoint,
    DftPcaSampleResponse,
)
from app.services.dft_repository import (
    count_dft_molecules,
    get_energy_trace,
    get_molecule_final,
    parse_coordinates,
    sample_pca_points,
)


router = APIRouter(prefix="/api/v1/dft", tags=["dft"])


@router.get("/pca-sample", response_model=DftPcaSampleResponse)
async def get_pca_sample(
    request: Request,
    limit: int = Query(default=200, ge=1, le=5000),
) -> DftPcaSampleResponse:
    started_at = perf_counter()
    settings = request.app.state.settings

    with request.app.state.sqlite_connection_factory(settings.fumol_db_file) as connection:
        total = count_dft_molecules(connection)
        rows = sample_pca_points(connection, limit=limit)

    return DftPcaSampleResponse(
        query_time_ms=(perf_counter() - started_at) * 1000,
        total=total,
        results=[
            DftPcaPoint(
                mol_id=row["mol_id"],
                x=float(row["pca_x"]),
                y=float(row["pca_y"]),
                z=float(row["pca_z"]),
                n_atoms=int(row["n_atoms"]),
                final_step=int(row["final_step"]),
                homo_ev=row["homo_ev"],
                lumo_ev=row["lumo_ev"],
                gap_ev=row["gap_ev"],
                dipole_moment=row["dipole_moment"],
            )
            for row in rows
        ],
    )


@router.get("/molecule/{mol_id}", response_model=DftMoleculeDetailResponse)
async def get_dft_molecule(mol_id: str, request: Request) -> DftMoleculeDetailResponse:
    settings = request.app.state.settings

    with request.app.state.sqlite_connection_factory(settings.fumol_db_file) as connection:
        row = get_molecule_final(connection, mol_id)
        if row is None:
            raise HTTPException(status_code=404, detail="DFT molecule not found")
        trace_rows = get_energy_trace(connection, mol_id)

    return DftMoleculeDetailResponse(
        mol_id=row["mol_id"],
        range_group=row["range_group"],
        final_step=int(row["final_step"]),
        n_atoms=int(row["n_atoms"]),
        coordinates=parse_coordinates(row["coordinates"]),
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
        trace=[
            DftEnergyPoint(
                step=int(trace_row["step"]),
                scf_energy=trace_row["scf_energy"],
                homo_ev=trace_row["homo_ev"],
                lumo_ev=trace_row["lumo_ev"],
                gap_ev=trace_row["gap_ev"],
            )
            for trace_row in trace_rows
        ],
    )
