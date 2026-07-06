from __future__ import annotations

import pytest

from app.postgres_database import postgres_connection
from app.services.similarity import similarity_search_postgres
from app.utils.exceptions import InvalidSmilesError


def test_similarity_search_returns_sorted_matches(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        results = similarity_search_postgres(connection, "CCO", similarity_threshold=0.3, top_k=2)

    assert len(results) == 2
    assert results[0][0]["smiles"] == "CCO"
    assert results[0][1] == 1.0
    assert results[0][1] >= results[1][1]


def test_similarity_search_skips_unparseable_rows(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        results = similarity_search_postgres(connection, "CCO", similarity_threshold=0.0, top_k=10)

    assert all(row["smiles"] != "not-a-smiles" for row, _ in results)


def test_similarity_search_rejects_invalid_smiles(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        with pytest.raises(InvalidSmilesError):
            similarity_search_postgres(connection, "not-a-smiles")


def test_similarity_search_skips_blank_candidate_rows(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        connection.execute(
            """
            INSERT INTO core.polymers (polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (4, "blank", "", "", True),
        )
        results = similarity_search_postgres(connection, "CCO", similarity_threshold=0.0, top_k=10)

    assert all(row["smiles"] != "" for row, _ in results)