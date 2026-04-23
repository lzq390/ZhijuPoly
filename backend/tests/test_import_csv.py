from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from unittest.mock import patch

from app.config import PROJECT_ROOT, Settings
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
        {
            "polymer_name": "polymer_empty",
            "smiles": "",
            "property_category": "Other",
            "property_name": "Misc",
            "property_value": "0",
            "property_unit": "",
            "label_source": "exp",
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
    assert settings.csv_source_file.name == "polyprop_9_properties_clean.csv"


def test_settings_load_backend_dotenv(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "SQLITE_DB_PATH=tmp/custom.db",
                "CSV_SOURCE_PATH=tmp/source.csv",
                "ALLOWED_ORIGINS=http://localhost:9000,http://localhost:9001",
                "MODEL_ENABLED=true",
            ]
        ),
        encoding="utf-8",
    )

    with patch("app.config.DEFAULT_ENV_FILE", env_path):
        settings = Settings()

    assert settings.sqlite_db_file == PROJECT_ROOT / "tmp" / "custom.db"
    assert settings.csv_source_file == PROJECT_ROOT / "tmp" / "source.csv"
    assert settings.allowed_origins_list == ["http://localhost:9000", "http://localhost:9001"]
    assert settings.model_enabled is True


def test_import_csv_creates_schema_and_imports_data(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)

    stats = import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    assert db_path.exists()
    assert stats.polymer_count == 3
    assert stats.property_count == 4
    assert stats.parse_ok_count == 1
    assert stats.parse_fail_count == 2

    polymer_counts = fetch_one(
        db_path,
        "SELECT COUNT(*) AS total, SUM(rdkit_parse_ok) AS parse_ok_total FROM polymers",
    )
    assert polymer_counts["total"] == 3
    assert polymer_counts["parse_ok_total"] == 1

    parsed_polymer = fetch_one(
        db_path,
        "SELECT canonical_smiles, rdkit_parse_ok FROM polymers WHERE smiles = 'CCO'",
    )
    assert parsed_polymer["canonical_smiles"] == "CCO"
    assert parsed_polymer["rdkit_parse_ok"] == 1

    invalid_polymer = fetch_one(
        db_path,
        "SELECT canonical_smiles, rdkit_parse_ok FROM polymers WHERE smiles = 'not-a-smiles'",
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

    empty_polymer = fetch_one(
        db_path,
        "SELECT canonical_smiles, rdkit_parse_ok FROM polymers WHERE smiles = ''",
    )
    assert empty_polymer["canonical_smiles"] is None
    assert empty_polymer["rdkit_parse_ok"] == 0

    normalized_unit = fetch_one(
        db_path,
        "SELECT property_unit FROM properties WHERE property_name = 'Tg'",
    )
    assert normalized_unit["property_unit"] == "°C"


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
    assert counts["polymer_total"] == 3
    assert counts["property_total"] == 4


def test_import_normalizes_compound_celsius_units(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    csv_path.write_text(
        "\n".join(
            [
                "polymer_name,smiles,property_category,property_name,property_value,property_unit,label_source",
                "polymer_a,CCO,Thermal,Specific heat,1.2,cal/(g*C),exp",
            ]
        ),
        encoding="utf-8",
    )

    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    row = fetch_one(
        db_path,
        "SELECT property_unit FROM properties WHERE property_name = 'Specific heat'",
    )
    assert row["property_unit"] == "cal/(g·°C)"


def test_import_normalizes_common_nonstandard_units(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    csv_path.write_text(
        "\n".join(
            [
                "polymer_name,smiles,property_category,property_name,property_value,property_unit,label_source",
                "polymer_a,CCO,Optical,Contact angle,90,degree,exp",
                "polymer_a,CCO,Chemical,Solubility parameter,1.0,(J/cm3)1/2,exp",
                "polymer_a,CCO,Chemical,Diffusion coefficient,1.0,cm2/s,exp",
                "polymer_a,CCO,Chemical,Virial coefficient,1.0,cm3*mol/g2,exp",
                "polymer_a,CCO,Other,G value,1.0,events/100ev,exp",
                "polymer_a,CCO,Thermal,Time to loss,1.0,hour,exp",
                "polymer_a,CCO,Thermal,Half time,1.0,second,exp",
                "polymer_a,CCO,Mechanical,Creep rupture time,1.0,year,exp",
                "polymer_a,CCO,Electrical,Conductivity,1.0,1/(ohm*cm),exp",
                "polymer_a,CCO,Electrical,Resistivity,1.0,ohm*cm,exp",
                "polymer_a,CCO,Thermal,Thermal conductivity,1.0,W/(m*K),exp",
                "polymer_a,CCO,Chemical,Permeability,1.0,cm3(STP)cm/(cm2*s*Pa),exp",
                "polymer_a,CCO,Chemical,Solubility coefficient,1.0,cm3(STP)/(cm3*Pa),exp",
                "polymer_a,CCO,Other,Water vapor transmission,1.0,g*mil/(cm2*24h),exp",
            ]
        ),
        encoding="utf-8",
    )

    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    rows = sqlite3.connect(db_path).execute(
        "SELECT property_name, property_unit FROM properties ORDER BY rowid"
    ).fetchall()
    normalized = {name: unit for name, unit in rows}

    assert normalized["Contact angle"] == "°"
    assert normalized["Solubility parameter"] == "(J/cm^3)^1/2"
    assert normalized["Diffusion coefficient"] == "cm^2/s"
    assert normalized["Virial coefficient"] == "cm^3·mol/g^2"
    assert normalized["G value"] == "events/100 eV"
    assert normalized["Time to loss"] == "h"
    assert normalized["Half time"] == "s"
    assert normalized["Creep rupture time"] == "yr"
    assert normalized["Conductivity"] == "1/(Ω·cm)"
    assert normalized["Resistivity"] == "Ω·cm"
    assert normalized["Thermal conductivity"] == "W/(m·K)"
    assert normalized["Permeability"] == "cm^3(STP)·cm/(cm^2·s·Pa)"
    assert normalized["Solubility coefficient"] == "cm^3(STP)/(cm^3·Pa)"
    assert normalized["Water vapor transmission"] == "g·mil/(cm^2·24 h)"


def test_import_applies_canonical_unit_spellings(tmp_path: Path) -> None:
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    csv_path.write_text(
        "\n".join(
            [
                "polymer_name,smiles,property_category,property_name,property_value,property_unit,label_source",
                "polymer_a,CCO,Chemical,Intrinsic viscosity,1.0,dl/g,exp",
                "polymer_a,CCO,Mechanical,Compressibility,1.0,1/(GPa),exp",
            ]
        ),
        encoding="utf-8",
    )

    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    rows = sqlite3.connect(db_path).execute(
        "SELECT property_name, property_unit FROM properties ORDER BY rowid"
    ).fetchall()
    normalized = {name: unit for name, unit in rows}

    assert normalized["Intrinsic viscosity"] == "dL/g"
    assert normalized["Compressibility"] == "1/GPa"
