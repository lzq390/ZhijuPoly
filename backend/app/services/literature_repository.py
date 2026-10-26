"""Read published literature projections from the application database only."""
from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.models import KnowledgeSearchRequest
from app.services.knowledge_search import _escape_like, parse_search_groups
from app.services.literature_normalizer import SUPPORTED_NORMALIZER_VERSIONS

DOI = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/\S+)$", re.IGNORECASE)
LITERATURE_FIELDS = (("title", "Title", 4), ("keywords_text", "Keywords", 2), ("abstract", "Abstract", 1))


def literature_error(status: int, code: str, message: str):
    return HTTPException(status, {"code": code, "message": message})


def normalize_doi_query(value: str) -> str | None:
    match = DOI.fullmatch(value.strip())
    return match.group(1).lower() if match else None


def published_generation(connection: Any, generation_id: str | None = None) -> str:
    if generation_id is not None:
        try:
            generation_id = str(UUID(generation_id))
        except ValueError as exc:
            raise literature_error(422, "LITERATURE_INVALID_GENERATION", "Invalid literature generation ID") from exc
        row = connection.execute(
            "SELECT generation_id,normalizer_version FROM knowledge.literature_generations WHERE generation_id=%s::uuid AND status='published'",
            (generation_id,),
        ).fetchone()
        if row is None:
            raise literature_error(404, "LITERATURE_GENERATION_NOT_FOUND", "Published literature generation not found")
    else:
        row = connection.execute(
            "SELECT generation_id,normalizer_version FROM knowledge.literature_generations WHERE is_active AND status='published'"
        ).fetchone()
        if row is None:
            raise literature_error(503, "LITERATURE_NOT_PUBLISHED", "Literature collection has not been published")
    if row["normalizer_version"] not in SUPPORTED_NORMALIZER_VERSIONS:
        raise literature_error(503, "LITERATURE_FORMAT_UNSUPPORTED", "Published literature format is not supported by this release")
    return str(row["generation_id"])


def literature_search_groups(body: KnowledgeSearchRequest) -> tuple[str | None, list[list[str]]]:
    doi = normalize_doi_query(body.query)
    if doi:
        return doi, [[body.query]]
    raw = ([group.terms for group in body.groups] if body.groups else
           [[term] for term in body.terms] if body.terms else parse_search_groups(body.query))
    # Literature phrases have no implicit IUPAC expansion.
    return None, raw


def search_literature(connection: Any, body: KnowledgeSearchRequest, groups: list[list[str]], doi: str | None):
    generation = published_generation(connection, body.generation_id)
    where = ["generation_id=%s::uuid"]
    params: list[Any] = [generation]
    scores: list[str] = []
    score_params: list[Any] = []
    if doi is not None:
        where.append("doi=%s")
        params.append(doi)
    else:
        for group in groups:
            alternatives = []
            for term in group:
                alternatives.append("(" + " OR ".join(f"{column} ILIKE %s ESCAPE '\\'" for column, _, _ in LITERATURE_FIELDS) + ")")
                params.extend([f"%{_escape_like(term)}%"] * len(LITERATURE_FIELDS))
            where.append("(" + " OR ".join(alternatives) + ")")
            for column, _, weight in LITERATURE_FIELDS:
                scores.append("CASE WHEN (" + " OR ".join(f"{column} ILIKE %s ESCAPE '\\'" for _ in group) + f") THEN {weight} ELSE 0 END")
                score_params.extend(f"%{_escape_like(term)}%" for term in group)
    filters = body.filters
    if filters is not None:
        for field, operator in (("year_from", ">="), ("year_to", "<=")):
            value = getattr(filters, field)
            if value is not None:
                where.append(f"publication_year {operator} %s")
                params.append(value)
        for field in ("has_properties", "has_processes", "has_original"):
            value = getattr(filters, field)
            if value is not None:
                where.append("COALESCE((capabilities->>%s)::boolean,false)=%s")
                params.extend((field, value))
    where_sql = " AND ".join(where)
    total = connection.execute(f"SELECT count(*) AS total FROM knowledge.literature_catalog WHERE {where_sql}", params).fetchone()["total"]
    page_size = body.page_size or body.top_k
    score = " + ".join(scores) if scores else "0"
    rows = connection.execute(
        f"""SELECT document_uid, doi, title, authors, journal, publication_year, keywords,
            keywords_text, abstract, capabilities, counts, snapshot_uid, ({score}) AS match_score
            FROM knowledge.literature_catalog WHERE {where_sql}
            ORDER BY match_score DESC, publication_year DESC NULLS LAST, document_uid
            LIMIT %s OFFSET %s""",
        [*score_params, *params, page_size, (body.page - 1) * page_size],
    ).fetchall()
    return generation, int(total), list(rows)


def literature_detail(connection: Any, document_uid: str, generation_id: str, snapshot_uid: str):
    generation = published_generation(connection, generation_id)
    row = connection.execute(
        """SELECT c.document_uid,c.doi,c.title,c.authors,c.journal,c.publication_year,c.keywords,c.abstract,
            c.capabilities,c.counts,s.snapshot_uid,s.source_paper_id,s.source_extract_id,
            s.source_extract_created_at,s.model_version,s.normalizer_version,s.content_sha256,
            s.normalized_json,s.assets,s.quality_flags
            FROM knowledge.literature_catalog c JOIN knowledge.literature_snapshots s
            ON s.document_uid=c.document_uid AND s.snapshot_uid=c.snapshot_uid
            WHERE c.generation_id=%s::uuid AND c.document_uid=%s AND c.snapshot_uid=%s""",
        (generation, document_uid, snapshot_uid),
    ).fetchone()
    if row is None:
        raise literature_error(404, "LITERATURE_DOCUMENT_NOT_FOUND", "Literature document snapshot not found in this generation")
    if row["normalizer_version"] not in SUPPORTED_NORMALIZER_VERSIONS:
        raise literature_error(503, "LITERATURE_FORMAT_UNSUPPORTED", "Literature snapshot format is not supported by this release")
    return dict(row)
