from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, Request

from app.models import KnowledgeDocumentResult, KnowledgeSearchRequest, KnowledgeSearchResponse
from app.services.knowledge_search import (
    best_abstract_snippet_query,
    build_abstract_snippet,
    get_knowledge_match_metadata,
    normalize_search_terms,
    search_knowledge_documents,
)


router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    request_body: KnowledgeSearchRequest,
    request: Request,
) -> KnowledgeSearchResponse:
    started_at = perf_counter()
    settings = request.app.state.settings
    search_terms = normalize_search_terms(request_body.query, request_body.terms)

    with request.app.state.sqlite_connection_factory(settings.sqlite_db_file) as connection:
        total, rows = search_knowledge_documents(
            connection,
            request_body.query,
            top_k=request_body.top_k,
            terms=search_terms,
        )

    elapsed_ms = (perf_counter() - started_at) * 1000
    results: list[KnowledgeDocumentResult] = []
    for row in rows:
        matched_terms, matched_fields = get_knowledge_match_metadata(row, search_terms)
        results.append(
            KnowledgeDocumentResult(
                knowledge_id=int(row["knowledge_id"]),
                source_file=row["source_file"],
                source_row_number=int(row["source_row_number"]),
                source_sequence=row["source_sequence"],
                title_zh=row["title_zh"],
                title_en=row["title_en"],
                abstract=row["abstract"],
                abstract_snippet=build_abstract_snippet(
                    row["abstract"],
                    best_abstract_snippet_query(row["abstract"], request_body.query, search_terms),
                ),
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
                matched_terms=matched_terms,
                matched_fields=matched_fields,
            )
        )

    return KnowledgeSearchResponse(
        query=request_body.query,
        terms=search_terms,
        query_time_ms=elapsed_ms,
        total=total,
        results=results,
    )
