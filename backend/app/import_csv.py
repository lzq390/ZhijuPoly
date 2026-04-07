from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

from rdkit import Chem
from rdkit import RDLogger

from app.config import get_settings
from app.database import rebuild_schema, sqlite_connection


RDLogger.DisableLog("rdApp.error")


@dataclass(slots=True)
class ImportStats:
    polymer_count: int
    property_count: int
    parse_ok_count: int
    parse_fail_count: int


def canonicalize_smiles(smiles: str) -> tuple[str | None, int]:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None, 0
    return Chem.MolToSmiles(mol), 1


def parse_float_or_none(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def import_csv_to_sqlite(csv_path: str | Path, db_path: str | Path) -> ImportStats:
    polymer_index: dict[tuple[str, str], int] = {}
    parse_ok_count = 0
    parse_fail_count = 0
    property_count = 0

    with sqlite_connection(db_path) as connection:
        rebuild_schema(connection)
        with Path(csv_path).open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                polymer_name = (row.get("polymer_name") or "").strip()
                smiles = (row.get("smiles") or "").strip()
                polymer_key = (polymer_name, smiles)

                polymer_id = polymer_index.get(polymer_key)
                if polymer_id is None:
                    canonical_smiles, rdkit_parse_ok = canonicalize_smiles(smiles)
                    if rdkit_parse_ok:
                        parse_ok_count += 1
                    else:
                        parse_fail_count += 1

                    cursor = connection.execute(
                        """
                        INSERT INTO polymers (
                            polymer_name,
                            smiles,
                            canonical_smiles,
                            rdkit_parse_ok
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (polymer_name, smiles, canonical_smiles, rdkit_parse_ok),
                    )
                    polymer_id = int(cursor.lastrowid)
                    polymer_index[polymer_key] = polymer_id

                connection.execute(
                    """
                    INSERT INTO properties (
                        polymer_id,
                        property_category,
                        property_name,
                        property_value,
                        property_value_num,
                        property_unit,
                        label_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        polymer_id,
                        (row.get("property_category") or "").strip(),
                        (row.get("property_name") or "").strip(),
                        (row.get("property_value") or "").strip(),
                        parse_float_or_none((row.get("property_value") or "").strip()),
                        ((row.get("property_unit") or "").strip() or None),
                        ((row.get("label_source") or "").strip() or None),
                    ),
                )
                property_count += 1

    return ImportStats(
        polymer_count=len(polymer_index),
        property_count=property_count,
        parse_ok_count=parse_ok_count,
        parse_fail_count=parse_fail_count,
    )


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Import polymer CSV into local SQLite database.")
    parser.add_argument("--csv-path", default=str(settings.csv_source_file))
    parser.add_argument("--db-path", default=str(settings.sqlite_db_file))
    args = parser.parse_args()

    stats = import_csv_to_sqlite(csv_path=args.csv_path, db_path=args.db_path)
    print(
        "Imported polymers={polymers} properties={properties} parse_ok={parse_ok} parse_fail={parse_fail}".format(
            polymers=stats.polymer_count,
            properties=stats.property_count,
            parse_ok=stats.parse_ok_count,
            parse_fail=stats.parse_fail_count,
        )
    )


if __name__ == "__main__":
    main()
