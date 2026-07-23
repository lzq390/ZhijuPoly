from __future__ import annotations

from typing import Any

from app.services.structure_similarity_index import StructureSimilarityIndex


def similarity_search_postgres(
    connection: Any,
    smiles: str,
    similarity_threshold: float = 0.7,
    top_k: int = 10,
    *,
    index: StructureSimilarityIndex | None = None,
) -> list[tuple[Any, float]]:
    search_index = index or StructureSimilarityIndex()
    return search_index.search(
        connection,
        smiles,
        similarity_threshold=similarity_threshold,
        top_k=top_k,
    )
