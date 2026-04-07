from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from app.config import Settings
from app.import_csv import import_csv_to_sqlite


def write_sample_csv(path: Path) -> None:
    rows = [
        {
            "polymer_name": "polymer_a",
            "smiles": "CCO",
            "property_category": "Thermal",
            "property_name": "Tg",
            "property_value": "123.4",
            "property_unit": "C",
            "label_source": "exp",
        },
        {
            "polymer_name": "polymer_a",
            "smiles": "CCO",
            "property_category": "Mechanical",
            "property_name": "Strength",
            "property_value": "10",
            "property_unit": "MPa",
            "label_source": "exp",
        },
        {
            "polymer_name": "polymer_b",
            "smiles": "not-a-smiles",
            "property_category": "Electrical",
            "property_name": "Conductivity",
            "property_value": "bad",
            "property_unit": "",
            "label_source": "calc",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def fetch_one(db_path: Path, query: str) -> sqlite3.Row:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query).fetchone()
    finally:
        connection.close()


def test_settings_resolve_paths_to_project_root() -> None:
    settings = Settings()
    assert settings.sqlite_db_file.is_absolute()
    assert settings.csv_source_file.is_absolute()
    assert settings.sqlite_db_file.name == "polyprop.db"
    assert settings.csv_source_file.name == "data1.csv"


def test_import_csv_creates_schema_and_imports_data(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)

    stats = import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    assert db_path.exists()
    assert stats.polymer_count == 2
    assert stats.property_count == 3
    assert stats.parse_ok_count == 1
    assert stats.parse_fail_count == 1

    polymer_counts = fetch_one(
        db_path,
        "SELECT COUNT(*) AS total, SUM(rdkit_parse_ok) AS parse_ok_total FROM polymers",
    )
    assert polymer_counts["total"] == 2
    assert polymer_counts["parse_ok_total"] == 1

    parsed_polymer = fetch_one(
        db_path,
        "SELECT canonical_smiles, rdkit_parse_ok FROM polymers WHERE polymer_name = 'polymer_a'",
    )
    assert parsed_polymer["canonical_smiles"] == "CCO"
    assert parsed_polymer["rdkit_parse_ok"] == 1

    invalid_polymer = fetch_one(
        db_path,
        "SELECT canonical_smiles, rdkit_parse_ok FROM polymers WHERE polymer_name = 'polymer_b'",
    )
    assert invalid_polymer["canonical_smiles"] is None
    assert invalid_polymer["rdkit_parse_ok"] == 0

    invalid_property = fetch_one(
        db_path,
        "SELECT property_value, property_value_num, property_unit FROM properties WHERE property_name = 'Conductivity'",
    )
    assert invalid_property["property_value"] == "bad"
    assert invalid_property["property_value_num"] is None
    assert invalid_property["property_unit"] is None


def test_reimport_rebuilds_database_without_accumulation(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)

    first_stats = import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)
    second_stats = import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    assert first_stats == second_stats

    counts = fetch_one(
        db_path,
        "SELECT (SELECT COUNT(*) FROM polymers) AS polymer_total, (SELECT COUNT(*) FROM properties) AS property_total",
    )
    assert counts["polymer_total"] == 2
    assert counts["property_total"] == 3
