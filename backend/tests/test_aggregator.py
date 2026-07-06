from __future__ import annotations

from app.postgres_database import postgres_connection
from app.services.aggregator import (
    build_polymer_result,
    fetch_property_rows_postgres,
    group_properties,
    load_polymer_results_postgres,
)


def test_group_properties_maps_known_categories(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        property_rows = fetch_property_rows_postgres(connection, 1)
        grouped = group_properties(property_rows)

    assert [item.property_name for item in grouped.thermal] == ["Tg", "Glass transition temperature"]
    assert [item.property_name for item in grouped.electrical] == ["Conductivity"]
    assert grouped.other == []


def test_group_properties_maps_unknown_categories_to_other(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        property_rows = fetch_property_rows_postgres(connection, 3)
        grouped = group_properties(property_rows)

    assert [item.property_name for item in grouped.other] == ["Misc"]


def test_build_polymer_result_includes_similarity_score(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        polymer_row = connection.execute(
            """
            SELECT polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok
            FROM core.polymers
            WHERE polymer_id = %s
            """,
            (1,),
        ).fetchone()
        property_rows = fetch_property_rows_postgres(connection, 1)
        result = build_polymer_result(polymer_row, property_rows, similarity_score=0.88)

    assert result.polymer_id == "1"
    assert result.polymer_name == "polymer_a"
    assert result.similarity_score == 0.88
    assert result.structure_svg is not None
    assert "<svg" in result.structure_svg
    assert result.properties.thermal[0].property_value_num == 123.4


def test_load_polymer_results_batches_rows(postgres_dsn: str) -> None:
    with postgres_connection(postgres_dsn) as connection:
        polymer_rows = connection.execute(
            """
            SELECT polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok
            FROM core.polymers
            WHERE polymer_id IN (%s, %s)
            ORDER BY polymer_id
            """,
            (1, 2),
        ).fetchall()
        results = load_polymer_results_postgres(connection, polymer_rows, {1: 1.0})

    assert len(results) == 2
    assert results[0].similarity_score == 1.0
    assert results[1].similarity_score is None