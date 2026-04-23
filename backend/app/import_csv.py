from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path

from rdkit import Chem
from rdkit import RDLogger

from app.config import get_settings
from app.database import rebuild_schema, sqlite_connection


RDLogger.DisableLog("rdApp.error")


CANONICAL_UNIT_MAP = {
    "%": "%",
    "(J/cm^3)^1/2": "(J/cm^3)^1/2",
    "1/(GPa)": "1/GPa",
    "1/(Ω·cm)": "1/(Ω·cm)",
    "GPa": "GPa",
    "MPa": "MPa",
    "Mrad": "Mrad",
    "W/(m·K)": "W/(m·K)",
    "cal/(g·°C)": "cal/(g·°C)",
    "cal/cm^3": "cal/cm^3",
    "cal/g": "cal/g",
    "cm^2/s": "cm^2/s",
    "cm^3(STP)/(cm^3·Pa)": "cm^3(STP)/(cm^3·Pa)",
    "cm^3(STP)cm/(cm^2·s·Pa)": "cm^3(STP)·cm/(cm^2·s·Pa)",
    "cm^3/g": "cm^3/g",
    "cm^3·mol/g^2": "cm^3·mol/g^2",
    "dl/g": "dL/g",
    "events/100 eV": "events/100 eV",
    "g/cm^3": "g/cm^3",
    "g/denier": "g/denier",
    "g·mil/(cm^2·24 h)": "g·mil/(cm^2·24 h)",
    "h": "h",
    "kJ/m": "kJ/m",
    "kJ/m^2": "kJ/m^2",
    "kcal/g": "kcal/g",
    "kcal/mol": "kcal/mol",
    "mN/m": "mN/m",
    "m^2/s": "m^2/s",
    "nm": "nm",
    "nm/s": "nm/s",
    "s": "s",
    "wt%": "wt%",
    "yr": "yr",
    "°": "°",
    "°C": "°C",
    "Ω": "Ω",
    "Ω·cm": "Ω·cm",
}


@dataclass(slots=True)
class ImportStats:
    polymer_count: int
    property_count: int
    parse_ok_count: int
    parse_fail_count: int


def canonicalize_smiles(smiles: str) -> tuple[str | None, int]:
    if not smiles.strip():
        return None, 0

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None, 0
    return Chem.MolToSmiles(mol), 1


def parse_float_or_none(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_property_unit(value: str) -> str | None:
    unit = value.strip()
    if not unit:
        return None

    lowered = unit.lower()
    direct_map = {
        "c": "°C",
        "deg c": "°C",
        "degree c": "°C",
        "degrees c": "°C",
        "℃": "°C",
        "°c": "°C",
        "degree": "°",
        "hour": "h",
        "second": "s",
        "year": "yr",
        "events/100ev": "events/100 eV",
    }
    if lowered in direct_map:
        return direct_map[lowered]

    normalized = unit.replace("*C)", "*°C)").replace("/C", "/°C")
    normalized = normalized.replace("*", "·")
    normalized = re.sub(r"(?i)\bohm\b", "Ω", normalized)
    normalized = re.sub(r"(\d)(h|s|yr)\b", r"\1 \2", normalized)

    exponent_replacements = {
        "cm2": "cm^2",
        "cm3": "cm^3",
        "m2": "m^2",
        "g2": "g^2",
    }
    for source, target in exponent_replacements.items():
        normalized = re.sub(rf"(?<!\^){re.escape(source)}", target, normalized)

    normalized = normalized.replace("(J/cm^3)1/2", "(J/cm^3)^1/2")
    return CANONICAL_UNIT_MAP.get(normalized, normalized)


def import_csv_to_sqlite(csv_path: str | Path, db_path: str | Path) -> ImportStats:
    polymer_index: dict[str, int] = {}
    parse_ok_count = 0
    parse_fail_count = 0
    property_count = 0

    with sqlite_connection(db_path) as connection:
        rebuild_schema(connection)
        with Path(csv_path).open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                smiles = (row.get("smiles") or "").strip()

                polymer_id = polymer_index.get(smiles)
                if polymer_id is None:
                    canonical_smiles, rdkit_parse_ok = canonicalize_smiles(smiles)
                    if rdkit_parse_ok:
                        parse_ok_count += 1
                    else:
                        parse_fail_count += 1

                    cursor = connection.execute(
                        """
                        INSERT INTO polymers (
                            smiles,
                            canonical_smiles,
                            rdkit_parse_ok
                        ) VALUES (?, ?, ?)
                        """,
                        (smiles, canonical_smiles, rdkit_parse_ok),
                    )
                    polymer_id = int(cursor.lastrowid)
                    polymer_index[smiles] = polymer_id

                connection.execute(
                    """
                    INSERT INTO properties (
                        polymer_id,
                        property_name,
                        property_value,
                        property_value_num,
                        property_unit,
                        label_source
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        polymer_id,
                        (row.get("property_name") or "").strip(),
                        (row.get("property_value") or "").strip(),
                        parse_float_or_none((row.get("property_value") or "").strip()),
                        normalize_property_unit(row.get("property_unit") or ""),
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
        f"Imported polymers={stats.polymer_count} properties={stats.property_count}"
        f" parse_ok={stats.parse_ok_count} parse_fail={stats.parse_fail_count}"
    )


if __name__ == "__main__":
    main()
