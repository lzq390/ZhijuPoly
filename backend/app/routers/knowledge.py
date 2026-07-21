from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.models import KnowledgeDocumentResult, KnowledgeSearchRequest, KnowledgeSearchResponse
from app.postgres_database import PostgresUnavailableError
from app.services.knowledge_search import (
    best_abstract_snippet_query,
    build_abstract_snippet,
    get_knowledge_match_metadata,
    normalize_search_terms,
)
from app.services.postgres_knowledge_search import search_knowledge_documents_postgres


router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])
POSTGRES_ONLY_DETAIL = "Postgres runtime is required; set STRUCTURED_DATA_BACKEND=postgres."


@router.post("/search", response_model=KnowledgeSearchResponse)
async def search_knowledge(
    request_body: KnowledgeSearchRequest,
    request: Request,
) -> KnowledgeSearchResponse:
    return await run_in_threadpool(_search_knowledge_sync, request_body, request.app)


def _search_knowledge_sync(
    request_body: KnowledgeSearchRequest,
    app,
) -> KnowledgeSearchResponse:
    started_at = perf_counter()
    settings = app.state.settings
    search_terms = normalize_search_terms(request_body.query, request_body.terms)
    page_size = request_body.page_size or request_body.top_k
    offset = (request_body.page - 1) * page_size

    if settings.structured_data_backend != "postgres":
        raise HTTPException(status_code=503, detail=POSTGRES_ONLY_DETAIL)

    try:
        with app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            total, rows = search_knowledge_documents_postgres(
                connection,
                request_body.query,
                top_k=page_size,
                offset=offset,
                terms=search_terms,
            )
    except PostgresUnavailableError as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL database is not reachable") from exc

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
        page=request_body.page,
        page_size=page_size,
        query_time_ms=elapsed_ms,
        total=total,
        results=results,
    )
