from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, HTTPException, Request

from app.models import (
    PolymerResult,
    SmilesQueryRequest,
    SmilesQueryResponse,
    Structure3DRequest,
    Structure3DResponse,
)
from app.services.aggregator import load_polymer_result, load_polymer_results
from app.services.matcher import exact_match
from app.services.similarity import similarity_search
from app.services.structure_3d import generate_3d_molblock
from app.utils.exceptions import InvalidSmilesError


router = APIRouter(prefix="/api/v1", tags=["query"])


@router.post("/query/smiles", response_model=SmilesQueryResponse)
async def query_smiles(request_body: SmilesQueryRequest, request: Request) -> SmilesQueryResponse:
    started_at = perf_counter()
    settings = request.app.state.settings

    try:
        with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
            if request_body.match_mode == "exact":
                matched_rows = exact_match(connection, request_body.smiles)
                similarity_scores = {
                    int(row["polymer_id"]): 1.0 for row in matched_rows
                }
                results = load_polymer_results(
                    connection,
                    matched_rows,
                    similarity_scores=similarity_scores,
                )
            else:
                matched_rows_with_scores = similarity_search(
                    connection,
                    request_body.smiles,
                    similarity_threshold=request_body.similarity_threshold,
                    top_k=request_body.top_k,
                )
                polymer_rows = [row for row, _ in matched_rows_with_scores]
                similarity_scores = {
                    int(row["polymer_id"]): score for row, score in matched_rows_with_scores
                }
                results = load_polymer_results(
                    connection,
                    polymer_rows,
                    similarity_scores=similarity_scores,
                )
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    elapsed_ms = (perf_counter() - started_at) * 1000
    return SmilesQueryResponse(
        match_type=request_body.match_mode,
        query_time_ms=elapsed_ms,
        total=len(results),
        results=results,
    )


@router.get("/polymer/{polymer_id}", response_model=PolymerResult)
async def get_polymer_detail(polymer_id: int, request: Request) -> PolymerResult:
    settings = request.app.state.settings

    with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
        polymer_row = connection.execute(
            """
            SELECT polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok
            FROM polymers
            WHERE polymer_id = ?
            """,
            (polymer_id,),
        ).fetchone()

        if polymer_row is None:
            raise HTTPException(status_code=404, detail="polymer not found")

        return load_polymer_result(connection, polymer_row)


@router.post("/structure/3d", response_model=Structure3DResponse)
async def generate_structure_3d(request_body: Structure3DRequest) -> Structure3DResponse:
    try:
        molblock, capped_smiles = generate_3d_molblock(request_body.smiles)
    except InvalidSmilesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return Structure3DResponse(
        molblock=molblock,
        capped_smiles=capped_smiles,
        format="mol",
    )
