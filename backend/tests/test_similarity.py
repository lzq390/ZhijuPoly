from __future__ import annotations

from pathlib import Path

import pytest

from app.database import sqlite_connection
from app.import_csv import import_csv_to_sqlite
from app.services.similarity import similarity_search
from app.utils.exceptions import InvalidSmilesError


def write_sample_csv(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "polymer_name,smiles,property_category,property_name,property_value,property_unit,label_source",
                "polymer_a,CCO,Thermal,Tg,123.4,C,exp",
                "polymer_b,CCN,Mechanical,Strength,10,MPa,exp",
                "polymer_c,c1ccccc1,Optical,RI,1.5,,calc",
                "polymer_bad,not-a-smiles,Electrical,Conductivity,bad,,exp",
            ]
        ),
        encoding="utf-8",
    )


def test_similarity_search_returns_sorted_matches(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)
    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    with sqlite_connection(db_path) as connection:
        results = similarity_search(connection, "CCO", similarity_threshold=0.3, top_k=2)

    assert len(results) == 2
    assert results[0][0]["smiles"] == "CCO"
    assert results[0][1] == 1.0
    assert results[0][1] >= results[1][1]


def test_similarity_search_skips_unparseable_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)
    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    with sqlite_connection(db_path) as connection:
        results = similarity_search(connection, "CCO", similarity_threshold=0.0, top_k=10)

    assert all(row["smiles"] != "not-a-smiles" for row, _ in results)


def test_similarity_search_rejects_invalid_smiles(tmp_path: Path) -> None:
    db_path = tmp_path / "sample.db"
    with sqlite_connection(db_path) as connection:
        with pytest.raises(InvalidSmilesError):
            similarity_search(connection, "not-a-smiles")


def test_similarity_search_skips_blank_candidate_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)
    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    with sqlite_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO polymers (smiles, canonical_smiles, rdkit_parse_ok)
            VALUES (?, ?, ?)
            """,
            ("", "", 1),
        )
        results = similarity_search(connection, "CCO", similarity_threshold=0.0, top_k=10)

    assert all(row["smiles"] != "" for row, _ in results)
