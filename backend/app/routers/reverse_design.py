from __future__ import annotations

import sqlite3
from time import perf_counter

from fastapi import APIRouter, HTTPException, Request

from app.models import (
    KnowledgeDocumentResult,
    ReverseDesignKnowledgeRequest,
    ReverseDesignKnowledgeResponse,
    ReverseDesignTgCandidate,
    ReverseDesignTgRequest,
    ReverseDesignTgResponse,
)
from app.services.knowledge_search import build_abstract_snippet, search_knowledge_documents
from app.services.reverse_design import get_pi_candidate, search_reverse_design_by_tg
from app.services.smiles_to_iupac import lookup_iupac_name
from app.services.structure_2d import generate_2d_svg
from app.utils.exceptions import InvalidSmilesError


router = APIRouter(prefix="/api/v1/reverse-design", tags=["reverse-design"])


def _database_not_initialized(exc: sqlite3.OperationalError) -> bool:
    return "no such table" in str(exc).lower()


def _to_knowledge_result(row: sqlite3.Row, query: str) -> KnowledgeDocumentResult:
    return KnowledgeDocumentResult(
        knowledge_id=int(row["knowledge_id"]),
        source_file=row["source_file"],
        source_row_number=int(row["source_row_number"]),
        source_sequence=row["source_sequence"],
        title_zh=row["title_zh"],
        title_en=row["title_en"],
        abstract=row["abstract"],
        abstract_snippet=build_abstract_snippet(row["abstract"], query),
        claim=row["claim"],
        analysis=row["analysis"],
        is_polymer_synthesis=row["is_polymer_synthesis"],
        judgement_reason=row["judgement_reason"],
        polymer_iupac=row["polymer_iupac"],
        formulation=row["formulation"],
        catalyst=row["catalyst"],
        temperature=row["temperature"],
        reaction_time=row["reaction_time"],
        solvent=row["solvent"],
    )


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


@router.post("/knowledge", response_model=ReverseDesignKnowledgeResponse)
async def search_candidate_knowledge(
    request_body: ReverseDesignKnowledgeRequest,
    request: Request,
) -> ReverseDesignKnowledgeResponse:
    settings = request.app.state.settings

    with request.app.state.sqlite_connection_factory(settings.pi_reverse_db_file) as pi_connection:
        candidate = get_pi_candidate(pi_connection, request_body.pi_id)
        if candidate is None:
            raise HTTPException(status_code=404, detail="PI candidate not found")

        monomer_a_smiles = candidate["mon1"]
        monomer_b_smiles = candidate["mon2"]
        monomer_a_iupac = lookup_iupac_name(pi_connection, monomer_a_smiles)
        monomer_b_iupac = lookup_iupac_name(pi_connection, monomer_b_smiles)

    query_terms = [value for value in (monomer_a_iupac, monomer_b_iupac) if value]
    knowledge_query = " ".join(query_terms) if query_terms else None
    knowledge: list[KnowledgeDocumentResult] | None = None

    if knowledge_query:
        with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
            _, rows = search_knowledge_documents(
                connection,
                knowledge_query,
                top_k=request_body.top_k,
            )
        knowledge = [_to_knowledge_result(row, knowledge_query) for row in rows]

    return ReverseDesignKnowledgeResponse(
        pi_id=request_body.pi_id,
        monomer_a_smiles=monomer_a_smiles,
        monomer_b_smiles=monomer_b_smiles,
        monomer_a_iupac=monomer_a_iupac,
        monomer_b_iupac=monomer_b_iupac,
        knowledge_query=knowledge_query,
        knowledge=knowledge,
    )
