from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.config import Settings
from app.import_csv import import_csv_to_sqlite
from app.main import create_app


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
            "property_category": "Electrical",
            "property_name": "Conductivity",
            "property_value": "1.5",
            "property_unit": "S/cm",
            "label_source": "exp",
        },
        {
            "polymer_name": "polymer_b",
            "smiles": "CCN",
            "property_category": "Mechanical",
            "property_name": "Strength",
            "property_value": "10",
            "property_unit": "MPa",
            "label_source": "calc",
        },
        {
            "polymer_name": "polymer_bad",
            "smiles": "not-a-smiles",
            "property_category": "Other",
            "property_name": "Misc",
            "property_value": "bad",
            "property_unit": "",
            "label_source": "exp",
        },
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def test_app(tmp_path: Path):
    csv_path = tmp_path / "sample.csv"
    db_path = tmp_path / "sample.db"
    write_sample_csv(csv_path)
    import_csv_to_sqlite(csv_path=csv_path, db_path=db_path)

    settings = Settings(
        sqlite_db_path=str(db_path),
        csv_source_path=str(csv_path),
        allowed_origins="http://localhost:5173",
        model_enabled=False,
    )
    app = create_app(settings)
    return app
