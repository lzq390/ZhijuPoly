from __future__ import annotations

from pathlib import Path

import pytest

from app.database import rebuild_schema, sqlite_connection
from app.import_csv import import_csv_to_sqlite
from app.services.matcher import exact_match
from app.utils.exceptions import InvalidSmilesError


def write_sample_csv(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "polymer_name,smiles,property_category,property_name,property_value,property_unit,label_source",
                "polymer_a,CCO,Thermal,Tg,123.4,C,exp",
                "polymer_b,CCN,Mechanical,Strength,10,MPa,exp",
            ]
        ),
        encoding="utf-8",
    )


def test_exact_match_prefers_canonical_smiles(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)
    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    with sqlite_connection(db_path) as connection:
        rows = exact_match(connection, "OCC")

    assert len(rows) == 1
    assert rows[0]["polymer_name"] == "polymer_a"


def test_exact_match_falls_back_to_raw_smiles(tmp_path: Path) -> None:
    db_path = tmp_path / "sample.db"
    with sqlite_connection(db_path) as connection:
        rebuild_schema(connection)
        connection.execute(
            """
            INSERT INTO polymers (polymer_name, smiles, canonical_smiles, rdkit_parse_ok)
            VALUES (?, ?, ?, ?)
            """,
            ("polymer_raw_only", "CCO", None, 0),
        )

        rows = exact_match(connection, "CCO")

    assert len(rows) == 1
    assert rows[0]["polymer_name"] == "polymer_raw_only"


def test_exact_match_returns_empty_when_not_found(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)
    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    with sqlite_connection(db_path) as connection:
        rows = exact_match(connection, "CCCC")

    assert rows == []


def test_exact_match_rejects_invalid_smiles(tmp_path: Path) -> None:
    db_path = tmp_path / "sample.db"
    with sqlite_connection(db_path) as connection:
        with pytest.raises(InvalidSmilesError):
            exact_match(connection, "not-a-smiles")
