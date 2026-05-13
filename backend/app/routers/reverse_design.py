from __future__ import annotations

import sqlite3
from time import perf_counter

from fastapi import APIRouter, HTTPException, Request

from app.models import (
    ReverseDesignTgCandidate,
    ReverseDesignTgRequest,
    ReverseDesignTgResponse,
)
from app.services.reverse_design import search_reverse_design_by_tg
from app.services.smiles_to_iupac import lookup_iupac_name
from app.services.structure_2d import generate_2d_svg
from app.utils.exceptions import InvalidSmilesError


router = APIRouter(prefix="/api/v1/reverse-design", tags=["reverse-design"])


def _database_not_initialized(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc).lower()


@router.post("/tg", response_model=ReverseDesignTgResponse)
async def search_by_tg(
    request_body: ReverseDesignTgRequest,
    request: Request,
) -> ReverseDesignTgResponse:
    started_at = perf_counter()
    settings = request.app.state.settings

    try:
        with request.app.state.sqlite_connection_factory(settings.pi_reverse_db_file) as connection:
            search_result = search_reverse_design_by_tg(
                connection,
                request_body.smiles,
                request_body.target_tg,
                similarity_threshold=request_body.similarity_threshold,
                candidate_sample_size=request_body.candidate_sample_size,
                top_k=request_body.top_k,
                random_seed=request_body.random_seed,
            )
            candidate_iupac = {
                candidate.pi_id: (
                    lookup_iupac_name(connection, candidate.monomer_a_smiles),
                    lookup_iupac_name(connection, candidate.monomer_b_smiles),
                )
                for candidate in search_result.results
            }
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except sqlite3.OperationalError as exc:
        if _database_not_initialized(exc):
            raise HTTPException(status_code=503, detail="PI reverse-design database is not initialized") from exc
        raise

    elapsed_ms = (perf_counter() - started_at) * 1000
    results = [
        ReverseDesignTgCandidate(
            rank=index + 1,
            pi_id=candidate.pi_id,
            polymer_smiles=candidate.polymer_smiles,
            canonical_polym=candidate.canonical_polym,
            monomer_a_smiles=candidate.monomer_a_smiles,
            monomer_b_smiles=candidate.monomer_b_smiles,
            monomer_a_iupac=candidate_iupac[candidate.pi_id][0],
            monomer_b_iupac=candidate_iupac[candidate.pi_id][1],
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
