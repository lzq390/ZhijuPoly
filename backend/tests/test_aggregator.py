from __future__ import annotations

from pathlib import Path

from app.database import sqlite_connection
from app.import_csv import import_csv_to_sqlite
from app.services.aggregator import build_polymer_result, fetch_property_rows, group_properties, load_polymer_results


def write_sample_csv(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "polymer_name,smiles,property_category,property_name,property_value,property_unit,label_source",
                "polymer_a,CCO,Thermal,Tg,123.4,C,exp",
                "polymer_a,CCO,Electrical,Conductivity,1.5,S/cm,exp",
                "polymer_a,CCO,Unknown,Misc,raw,,calc",
            ]
        ),
        encoding="utf-8",
    )


def test_group_properties_maps_unknown_categories_to_other(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)
    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    with sqlite_connection(db_path) as connection:
        polymer_row = connection.execute("SELECT * FROM polymers").fetchone()
        property_rows = fetch_property_rows(connection, int(polymer_row["polymer_id"]))
        grouped = group_properties(property_rows)

    assert grouped.thermal[0].property_name == "Tg"
    assert grouped.electrical[0].property_name == "Conductivity"
    assert grouped.other[0].property_name == "Misc"


def test_build_polymer_result_includes_similarity_score(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)
    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    with sqlite_connection(db_path) as connection:
        polymer_row = connection.execute("SELECT * FROM polymers").fetchone()
        property_rows = fetch_property_rows(connection, int(polymer_row["polymer_id"]))
        result = build_polymer_result(polymer_row, property_rows, similarity_score=0.88)

    assert result.polymer_id == str(polymer_row["polymer_id"])
    assert result.similarity_score == 0.88
    assert result.properties.thermal[0].property_value_num == 123.4


def test_load_polymer_results_batches_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)
    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    with sqlite_connection(db_path) as connection:
        polymer_rows = connection.execute("SELECT * FROM polymers ORDER BY polymer_id").fetchall()
        results = load_polymer_results(connection, polymer_rows, {int(polymer_rows[0]["polymer_id"]): 1.0})

    assert len(results) == 1
    assert results[0].similarity_score == 1.0
