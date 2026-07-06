from __future__ import annotations

from typing import Any

from app.services.knowledge_search import SEARCH_FIELDS, _escape_like, normalize_search_terms


def _field_like_sql(column: str) -> str:
    return f"COALESCE({column}, '') ILIKE %s ESCAPE '\\'"


def _build_search_sql_parts(terms: list[str]) -> tuple[str, list[str], str, list[str], str, list[str]]:
    where_parts: list[str] = []
    where_params: list[str] = []
    match_count_parts: list[str] = []
    match_count_params: list[str] = []
    score_parts: list[str] = []
    score_params: list[str] = []

    for term in terms:
        like_query = f"%{_escape_like(term)}%"
        term_parts: list[str] = []
        count_term_parts: list[str] = []

        for column, _, weight in SEARCH_FIELDS:
            field_sql = _field_like_sql(column)
            term_parts.append(field_sql)
            where_params.append(like_query)
            count_term_parts.append(field_sql)
            match_count_params.append(like_query)
            score_parts.append(f"CASE WHEN {field_sql} THEN {weight} ELSE 0 END")
            score_params.append(like_query)

        where_parts.append("(" + " OR ".join(term_parts) + ")")
        match_count_parts.append("CASE WHEN (" + " OR ".join(count_term_parts) + ") THEN 1 ELSE 0 END")

    where_sql = " OR ".join(where_parts) if where_parts else "false"
    match_count_sql = " + ".join(match_count_parts) if match_count_parts else "0"
    score_sql = " + ".join(score_parts) if score_parts else "0"
    return where_sql, where_params, match_count_sql, match_count_params, score_sql, score_params


def search_knowledge_documents_postgres(
    connection: Any,
    query: str,
    *,
    top_k: int,
    offset: int = 0,
    terms: list[str] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    search_terms = normalize_search_terms(query, terms)
    if not search_terms:
        return 0, []

    where_sql, where_params, match_count_sql, match_count_params, score_sql, score_params = _build_search_sql_parts(search_terms)

    total_row = connection.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM knowledge.documents
        WHERE {where_sql}
        """,
        where_params,
    ).fetchone()
    total = int(total_row["total"]) if total_row is not None else 0

    rows = connection.execute(
        f"""
        SELECT
            knowledge_id,
            source_file,
            source_row_number,
            source_sequence,
            title_zh,
            title_en,
            abstract,
            claim,
            analysis,
            is_polymer_synthesis,
            judgement_reason,
            polymer_iupac,
            formulation,
            catalyst,
            temperature,
            reaction_time,
            solvent,
            ({match_count_sql}) AS matched_term_count,
            ({score_sql}) AS match_score
        FROM knowledge.documents
        WHERE {where_sql}
        ORDER BY matched_term_count DESC, match_score DESC, knowledge_id ASC
        LIMIT %s OFFSET %s
        """,
        [*match_count_params, *score_params, *where_params, top_k, offset],
    ).fetchall()
    return total, list(rows)
