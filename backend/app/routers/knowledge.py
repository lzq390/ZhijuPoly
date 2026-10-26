from __future__ import annotations

from time import perf_counter
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.models import (
    KnowledgeDocumentResult,
    KnowledgeSearchGroup,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.postgres_database import PostgresUnavailableError
from app.recording_models import RecordedKnowledgeSearchRequest, RecordedKnowledgeSearchResponse
from app.services.knowledge_search import (
    KnowledgeSearchExpressionError,
    best_abstract_snippet_query,
    build_abstract_snippet,
    flatten_search_groups,
    get_knowledge_match_metadata,
    normalize_search_groups,
)
from app.services.postgres_knowledge_search import search_knowledge_documents_postgres
from app.services.literature_repository import (
    LITERATURE_FIELDS, literature_detail, literature_search_groups,
    literature_error, search_literature,
)
from app.services.literature_preflight import check_literature_readiness


class KnowledgeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def wrapped(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                literature = request.url.path.startswith("/api/v1/knowledge/literature/") or (
                    isinstance(exc.body, dict) and exc.body.get("data_source") == "extracted_literature")
                if not literature:
                    raise
                return JSONResponse(status_code=422, content={"detail": {
                    "code": "LITERATURE_INVALID_REQUEST", "message": "Invalid literature request parameters"}})
        return wrapped


router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"], route_class=KnowledgeRoute)
POSTGRES_ONLY_DETAIL = "Postgres runtime is required; set STRUCTURED_DATA_BACKEND=postgres."


@router.post("/search", response_model=RecordedKnowledgeSearchResponse)
async def search_knowledge(
    request_body: RecordedKnowledgeSearchRequest,
    request: Request,
) -> RecordedKnowledgeSearchResponse:
    # Existing Python callers still use the original request model.
    if not isinstance(request_body, RecordedKnowledgeSearchRequest):
        request_body = RecordedKnowledgeSearchRequest(**request_body.model_dump())
    return await request.app.state.browsing_recording.search(
        request_body, lambda: run_in_threadpool(_search_knowledge_sync, request_body, request.app)
    )


def _search_knowledge_sync(
    request_body: KnowledgeSearchRequest,
    app,
) -> KnowledgeSearchResponse:
    started_at = perf_counter()
    settings = app.state.settings
    if request_body.data_source == "extracted_literature":
        return _search_literature_sync(request_body, app, started_at)
    try:
        _, expanded_groups = normalize_search_groups(
            request_body.query,
            groups=[group.terms for group in request_body.groups] or None,
            terms=request_body.terms or None,
        )
    except KnowledgeSearchExpressionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    search_terms = flatten_search_groups(expanded_groups)
    page_size = request_body.page_size or request_body.top_k
    offset = (request_body.page - 1) * page_size

    if settings.structured_data_backend != "postgres":
        raise HTTPException(status_code=503, detail=POSTGRES_ONLY_DETAIL)

    try:
        with app.state.postgres_connection_factory(settings.app_postgres_dsn) as connection:
            total, rows = search_knowledge_documents_postgres(
                connection,
                expanded_groups,
                top_k=page_size,
                offset=offset,
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
        groups=[KnowledgeSearchGroup(terms=group) for group in expanded_groups],
        terms=search_terms,
        page=request_body.page,
        page_size=page_size,
        query_time_ms=elapsed_ms,
        total=total,
        results=results,
    )


def _require_literature(app):
    if not getattr(app.state.settings, "literature_enabled", False):
        raise literature_error(503, "LITERATURE_DISABLED", "Extracted literature is not enabled")
    if app.state.settings.structured_data_backend != "postgres":
        raise literature_error(503, "LITERATURE_BACKEND_REQUIRED", POSTGRES_ONLY_DETAIL)


@contextmanager
def _literature_connection(app):
    _require_literature(app)
    try:
        with app.state.postgres_connection_factory(app.state.settings.app_postgres_dsn) as connection:
            yield connection
    except PostgresUnavailableError as exc:
        if getattr(exc.__cause__, "sqlstate", None) in {"57014", "55P03"}:
            raise literature_error(504, "LITERATURE_QUERY_TIMEOUT", "Literature request timed out") from exc
        raise literature_error(503, "LITERATURE_DATABASE_UNAVAILABLE", "Literature database is not reachable") from exc
    except Exception as exc:
        if getattr(exc, "sqlstate", None) in {"42P01", "42703"}:
            raise literature_error(503, "LITERATURE_SCHEMA_NOT_READY", "Literature schema is not ready") from exc
        if getattr(exc, "sqlstate", None) == "42501":
            raise literature_error(503, "LITERATURE_API_PRIVILEGES_INVALID", "Literature read permissions are not ready") from exc
        if getattr(exc, "sqlstate", None) in {"57014", "55P03"}:
            raise literature_error(504, "LITERATURE_QUERY_TIMEOUT", "Literature request timed out") from exc
        raise


def _search_literature_sync(body, app, started_at):
    _require_literature(app)
    try:
        doi, groups = literature_search_groups(body)
    except KnowledgeSearchExpressionError as exc:
        raise literature_error(422, "LITERATURE_QUERY_INVALID", str(exc)) from exc
    terms = flatten_search_groups(groups)
    with _literature_connection(app) as connection:
        generation, total, rows = search_literature(connection, body, groups, doi)
    results = []
    for row in rows:
        matched_terms, matched_fields = [], []
        for term in terms:
            for column, label, _ in LITERATURE_FIELDS:
                if term.casefold() in str(row.get(column) or "").casefold():
                    if term not in matched_terms:
                        matched_terms.append(term)
                    if label not in matched_fields:
                        matched_fields.append(label)
        if doi:
            matched_terms, matched_fields = [body.query], ["DOI"]
        abstract = row["abstract"] or ""
        results.append(KnowledgeDocumentResult(
            document_uid=row["document_uid"], data_source="extracted_literature",
            generation_id=generation, snapshot_uid=row["snapshot_uid"],
            title=row["title"], title_en=row["title"], doi=row["doi"],
            authors=row["authors"] or [], journal=row["journal"], year=row["publication_year"],
            keywords=row["keywords"] or [], abstract=abstract,
            abstract_snippet=build_abstract_snippet(abstract, best_abstract_snippet_query(abstract, body.query, terms)),
            capabilities=row["capabilities"] or {}, counts=row["counts"] or {},
            matched_terms=matched_terms, matched_fields=matched_fields,
        ))
    return KnowledgeSearchResponse(
        query=body.query, data_source="extracted_literature", generation_id=generation,
        groups=[KnowledgeSearchGroup(terms=group) for group in groups], terms=terms,
        page=body.page, page_size=body.page_size or body.top_k,
        query_time_ms=(perf_counter() - started_at) * 1000, total=total, results=results,
    )


@router.get("/sources")
async def get_knowledge_sources(request: Request):
    return await run_in_threadpool(_sources_sync, request.app)


def _sources_sync(app):
    legacy = {"data_source": "legacy", "enabled": True, "ready": False, "total": 0,
              "generation_id": None, "published_at": None}
    enabled = bool(getattr(app.state.settings, "literature_enabled", False))
    literature = {"data_source": "extracted_literature", "enabled": enabled, "ready": False,
                  "total": 0, "generation_id": None, "published_at": None,
                  "reason": "LITERATURE_DISABLED" if not enabled else "LITERATURE_NOT_PUBLISHED"}
    try:
        with app.state.postgres_connection_factory(app.state.settings.app_postgres_dsn) as connection:
            legacy["total"] = int(connection.execute("SELECT count(*) AS total FROM knowledge.documents").fetchone()["total"])
            legacy["ready"] = True
    except Exception:
        legacy["reason"] = "unavailable"
    if enabled:
        try:
            with _literature_connection(app) as connection:
                state = check_literature_readiness(connection)
            literature.update(state)
        except HTTPException as exc:
            literature["reason"] = exc.detail.get("code", "LITERATURE_UNAVAILABLE") if isinstance(exc.detail, dict) else "LITERATURE_UNAVAILABLE"
    return {"sources": [legacy, literature]}


def _read_literature_row(app, document_uid, generation_id, snapshot_uid):
    with _literature_connection(app) as connection:
        return literature_detail(connection, document_uid, generation_id, snapshot_uid)


@router.get("/literature/{document_uid}")
async def get_literature_document(
    document_uid: str, request: Request,
    generation_id: str = Query(min_length=1, max_length=64),
    snapshot_uid: str = Query(min_length=1, max_length=200),
):
    from app.services.literature_assets import get_literature_asset_summary
    row = await run_in_threadpool(_read_literature_row, request.app, document_uid, generation_id, snapshot_uid)
    assets = get_literature_asset_summary(row["assets"] or [])
    return {
        "document": {"document_uid": row["document_uid"], "title": row["title"], "doi": row["doi"],
                     "authors": row["authors"], "journal": row["journal"], "year": row["publication_year"],
                     "keywords": row["keywords"], "abstract": row["abstract"]},
        "generation_id": generation_id, "snapshot_uid": snapshot_uid,
        "extraction": {key: row[key] for key in ("source_paper_id", "source_extract_id", "source_extract_created_at",
                       "model_version", "normalizer_version", "content_sha256")},
        "samples": (row["normalized_json"] or {}).get("samples", []),
        "quality_flags": row["quality_flags"] or [], "assets": assets["assets"],
        "capabilities": {**(row["capabilities"] or {}), **assets["capabilities"]},
    }


@router.get("/literature/{document_uid}/fulltext")
async def get_literature_fulltext_route(
    document_uid: str, request: Request,
    generation_id: str = Query(min_length=1, max_length=64),
    snapshot_uid: str = Query(min_length=1, max_length=200),
    cursor: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=100, ge=1, le=200),
):
    from app.services.literature_assets import get_literature_fulltext
    row = await run_in_threadpool(_read_literature_row, request.app, document_uid, generation_id, snapshot_uid)
    result = await get_literature_fulltext(assets_manifest=row["assets"] or [], document_uid=document_uid,
                                          snapshot_uid=snapshot_uid, cursor=cursor, limit=limit)
    return {**result, "generation_id": generation_id}


@router.api_route("/literature/{document_uid}/files/{asset_uid}/content", methods=["GET", "HEAD"])
async def get_literature_file(
    document_uid: str, asset_uid: str, request: Request,
    generation_id: str = Query(min_length=1, max_length=64),
    snapshot_uid: str = Query(min_length=1, max_length=200),
):
    from app.services.literature_assets import get_literature_file_response
    row = await run_in_threadpool(_read_literature_row, request.app, document_uid, generation_id, snapshot_uid)
    return await get_literature_file_response(assets_manifest=row["assets"] or [], file_uid=asset_uid, request=request)
