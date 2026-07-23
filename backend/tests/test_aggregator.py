from __future__ import annotations

from app.postgres_database import postgres_connection
from app.services import aggregator
from app.services.aggregator import (
    _generate_2d_svg_cached,
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

        class CountingConnection:
            def __init__(self, wrapped) -> None:
                self.wrapped = wrapped
                self.executions: list[tuple[str, object]] = []

            def execute(self, query: str, params):
                self.executions.append((query, params))
                return self.wrapped.execute(query, params)

        counting_connection = CountingConnection(connection)
        results = load_polymer_results_postgres(counting_connection, polymer_rows, {1: 1.0})

    assert len(results) == 2
    assert results[0].similarity_score == 1.0
    assert results[1].similarity_score is None
    assert len(counting_connection.executions) == 1
    assert "polymer_id = ANY" in counting_connection.executions[0][0]
    assert counting_connection.executions[0][1] == ([1, 2],)


def test_build_polymer_result_reuses_cached_structure_svg(
    postgres_dsn: str,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_generate_2d_svg(smiles: str) -> str:
        calls.append(smiles)
        return f"<svg>{smiles}</svg>"

    monkeypatch.setattr(aggregator, "generate_2d_svg", fake_generate_2d_svg)
    _generate_2d_svg_cached.cache_clear()
    try:
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

        first = build_polymer_result(polymer_row, property_rows)
        second = build_polymer_result(polymer_row, property_rows)

        assert first.structure_svg == second.structure_svg
        assert calls == [polymer_row["canonical_smiles"]]
    finally:
        _generate_2d_svg_cached.cache_clear()
