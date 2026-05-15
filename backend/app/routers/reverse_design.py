from __future__ import annotations

import sqlite3
from time import perf_counter
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from app.models import (
    ReverseDesignTgCandidate,
    ReverseDesignTgJobCreateResponse,
    ReverseDesignTgJobStatusResponse,
    ReverseDesignTgRequest,
    ReverseDesignTgResponse,
)
from app.postgres_database import PostgresUnavailableError
from app.services.postgres_reverse_design import search_reverse_design_by_tg_postgres
from app.services.fingerprint import generate
from app.services.reverse_design import search_reverse_design_by_tg
from app.services.smiles_to_iupac import lookup_iupac_name
from app.services.structure_2d import generate_2d_svg
from app.utils.exceptions import InvalidSmilesError


router = APIRouter(prefix="/api/v1/reverse-design", tags=["reverse-design"])
ProgressCallback = Callable[..., None]
CancellationCheck = Callable[[], bool]


def _database_not_initialized(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc).lower()


def _candidate_iupac_from_postgres(search_result) -> dict[int, tuple[str | None, str | None]]:
    return {
        candidate.pi_id: (candidate.monomer_a_iupac, candidate.monomer_b_iupac)
        for candidate in search_result.results
    }


def _validate_query_smiles(smiles: str) -> None:
    try:
        generate(smiles.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _build_tg_response(
    request_body: ReverseDesignTgRequest,
    search_result: Any,
    candidate_iupac: dict[int, tuple[str | None, str | None]],
    elapsed_ms: float,
) -> ReverseDesignTgResponse:
    results = [
        ReverseDesignTgCandidate(
            rank=index + 1,
            pi_id=candidate.pi_id,
            polymer_smiles=candidate.polymer_smiles,
            canonical_polym=candidate.canonical_polym,
            monomer_a_smiles=candidate.monomer_a_smiles,
            monomer_b_smiles=candidate.monomer_b_smiles,
            monomer_a_iupac=candidate_iupac.get(candidate.pi_id, (None, None))[0],
            monomer_b_iupac=candidate_iupac.get(candidate.pi_id, (None, None))[1],
            monomer_a_structure_svg=generate_2d_svg(candidate.monomer_a_smiles),
            monomer_b_structure_svg=generate_2d_svg(candidate.monomer_b_smiles),
            tg_value=candidate.tg_value,
            tg_difference=candidate.tg_difference,
            similarity_score=candidate.similarity_score,
            structure_svg=generate_2d_svg(candidate.canonical_polym or candidate.polymer_smiles),
            knowledge_available=bool(candidate.monomer_a_smiles or candidate.monomer_b_smiles),
        )
        for index, candidate in enumerate(search_result.results)
    ]

    return ReverseDesignTgResponse(
        target_tg=request_body.target_tg,
        query_time_ms=elapsed_ms,
        candidate_pool_size=search_result.candidate_pool_size,
        sampled_candidate_count=search_result.sampled_candidate_count,
        total=len(results),
        results=results,
    )


def _search_by_tg_response(
    request_body: ReverseDesignTgRequest,
    app: Any,
    *,
    full_scan: bool = False,
    progress_callback: ProgressCallback | None = None,
    cancellation_check: CancellationCheck | None = None,
) -> ReverseDesignTgResponse:
    started_at = perf_counter()
    settings = app.state.settings

    try:
        if settings.pi_reverse_backend == "postgres":
            def forward_progress(progress) -> None:
                if progress_callback is None:
                    return
                progress_callback(
                    scanned_rows=progress.scanned_rows,
                    matched_count=progress.matched_count,
                    current_tg_radius=progress.current_tg_radius,
                    best_similarity_score=progress.best_similarity_score,
                )

            with app.state.postgres_connection_factory(settings.pi_postgres_dsn) as connection:
                search_kwargs: dict[str, Any] = {
                    "similarity_threshold": request_body.similarity_threshold,
                    "result_limit": request_body.candidate_size,
                }
                if full_scan:
                    search_kwargs.update(
                        {
                            "batch_size": settings.pi_reverse_job_batch_size,
                            "max_scan_rows": None,
                            "timeout_seconds": 0,
                            "progress_callback": forward_progress,
                            "progress_interval_rows": settings.pi_reverse_progress_interval_rows,
                            "cancellation_check": cancellation_check,
                        }
                    )
                else:
                    search_kwargs.update(
                        {
                            "max_scan_rows": settings.pi_reverse_max_scan_rows,
                            "timeout_seconds": settings.pi_reverse_timeout_seconds,
                        }
                    )
                search_result = search_reverse_design_by_tg_postgres(
                    connection,
                    request_body.smiles,
                    request_body.target_tg,
                    **search_kwargs,
                )
                candidate_iupac = _candidate_iupac_from_postgres(search_result)
        else:
            with app.state.sqlite_connection_factory(settings.pi_reverse_db_file) as connection:
                search_result = search_reverse_design_by_tg(
                    connection,
                    request_body.smiles,
                    request_body.target_tg,
                    similarity_threshold=request_body.similarity_threshold,
                    candidate_sample_size=request_body.candidate_size,
                    top_k=request_body.candidate_size,
                    progress_callback=progress_callback,
                    progress_interval_rows=settings.pi_reverse_progress_interval_rows,
                    cancellation_check=cancellation_check,
                )
                candidate_iupac = {
                    candidate.pi_id: (
                        candidate.monomer_a_iupac
                        or lookup_iupac_name(connection, candidate.monomer_a_smiles),
                        candidate.monomer_b_iupac
                        or lookup_iupac_name(connection, candidate.monomer_b_smiles),
                    )
                    for candidate in search_result.results
                }
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except sqlite3.OperationalError as exc:
        if _database_not_initialized(exc):
            raise HTTPException(status_code=503, detail="PI reverse-design database is not initialized") from exc
        raise

    if progress_callback is not None:
        progress_callback(
            scanned_rows=search_result.scanned_rows,
            matched_count=search_result.candidate_pool_size,
            current_tg_radius=search_result.current_tg_radius,
            best_similarity_score=search_result.best_similarity_score,
        )

    elapsed_ms = (perf_counter() - started_at) * 1000
    return _build_tg_response(request_body, search_result, candidate_iupac, elapsed_ms)


@router.post("/tg", response_model=ReverseDesignTgResponse)
async def search_by_tg(
    request_body: ReverseDesignTgRequest,
    request: Request,
) -> ReverseDesignTgResponse:
    return _search_by_tg_response(request_body, request.app)


@router.post("/tg/jobs", response_model=ReverseDesignTgJobCreateResponse, status_code=202)
async def create_tg_search_job(
    request_body: ReverseDesignTgRequest,
    request: Request,
) -> ReverseDesignTgJobCreateResponse:
    _validate_query_smiles(request_body.smiles)
    manager = request.app.state.reverse_design_job_manager

    def run_search(
        progress_callback: ProgressCallback,
        cancellation_check: CancellationCheck,
    ) -> ReverseDesignTgResponse:
        return _search_by_tg_response(
            request_body,
            request.app,
            full_scan=True,
            progress_callback=progress_callback,
            cancellation_check=cancellation_check,
        )

    job = manager.create_job(request_body, run_search)
    return ReverseDesignTgJobCreateResponse(job_id=job.job_id, status=job.status)


@router.get("/tg/jobs/{job_id}", response_model=ReverseDesignTgJobStatusResponse)
async def get_tg_search_job(job_id: str, request: Request) -> ReverseDesignTgJobStatusResponse:
    manager = request.app.state.reverse_design_job_manager
    try:
        return manager.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
