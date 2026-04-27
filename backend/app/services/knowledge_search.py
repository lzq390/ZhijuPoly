from __future__ import annotations

import sqlite3

from app.database import ensure_knowledge_schema


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_abstract_snippet(abstract: str, query: str, context_size: int = 130) -> str:
    normalized_abstract = abstract.casefold()
    normalized_query = query.casefold()
    match_index = normalized_abstract.find(normalized_query)
    if match_index < 0:
        return abstract[: context_size * 2].strip()

    start = max(match_index - context_size, 0)
    end = min(match_index + len(query) + context_size, len(abstract))
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(abstract) else ""
    return f"{prefix}{abstract[start:end].strip()}{suffix}"


def search_knowledge_documents(
    connection: sqlite3.Connection,
    query: str,
    *,
    top_k: int,
) -> tuple[int, list[sqlite3.Row]]:
    ensure_knowledge_schema(connection)
    like_query = f"%{_escape_like(query)}%"

    total_row = connection.execute(
        """
        SELECT COUNT(*) AS total
        FROM knowledge_documents
        WHERE abstract COLLATE NOCASE LIKE ? ESCAPE '\\'
        """,
        (like_query,),
    ).fetchone()
    total = int(total_row["total"]) if total_row is not None else 0

    rows = connection.execute(
        """
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
            solvent
        FROM knowledge_documents
        WHERE abstract COLLATE NOCASE LIKE ? ESCAPE '\\'
        ORDER BY knowledge_id ASC
        LIMIT ?
        """,
        (like_query, top_k),
    ).fetchall()
    return total, rows
