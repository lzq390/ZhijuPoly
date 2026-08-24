from __future__ import annotations

from typing import Any

from app.services.knowledge_search import SEARCH_FIELDS, _escape_like


def _field_like_sql(column: str) -> str:
    # Keep the indexed column bare so PostgreSQL can use its gin_trgm_ops
    # index. NULL ILIKE yields NULL, which is already non-matching in WHERE,
    # OR, and CASE WHEN expressions.
    return f"{column} ILIKE %s ESCAPE '\\'"


def _build_search_sql_parts(groups: list[list[str]]) -> tuple[str, list[str], str, list[str]]:
    where_parts: list[str] = []
    where_params: list[str] = []
    score_parts: list[str] = []
    score_params: list[str] = []

    for group in groups:
        group_term_parts: list[str] = []
        for term in group:
            like_query = f"%{_escape_like(term)}%"
            term_field_parts: list[str] = []
            for column, _, _ in SEARCH_FIELDS:
                term_field_parts.append(_field_like_sql(column))
                where_params.append(like_query)
            group_term_parts.append("(" + " OR ".join(term_field_parts) + ")")
        where_parts.append("(" + " OR ".join(group_term_parts) + ")")

        # Each logical group contributes a field weight at most once, even when
        # multiple aliases or IUPAC expansions match the same field.
        for column, _, weight in SEARCH_FIELDS:
            field_term_parts: list[str] = []
            for term in group:
                field_term_parts.append(_field_like_sql(column))
                score_params.append(f"%{_escape_like(term)}%")
            score_parts.append(
                "CASE WHEN (" + " OR ".join(field_term_parts) + f") THEN {weight} ELSE 0 END"
            )

    where_sql = " AND ".join(where_parts) if where_parts else "false"
    score_sql = " + ".join(score_parts) if score_parts else "0"
    return where_sql, where_params, score_sql, score_params


def search_knowledge_documents_postgres(
    connection: Any,
    groups: list[list[str]],
    *,
    top_k: int,
    offset: int = 0,
) -> tuple[int, list[dict[str, Any]]]:
    if not groups:
        return 0, []

    where_sql, where_params, score_sql, score_params = _build_search_sql_parts(groups)

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
            ({score_sql}) AS match_score
        FROM knowledge.documents
        WHERE {where_sql}
        ORDER BY match_score DESC, knowledge_id ASC
        LIMIT %s OFFSET %s
        """,
        [*score_params, *where_params, top_k, offset],
    ).fetchall()
    return total, list(rows)
