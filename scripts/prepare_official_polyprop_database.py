from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.import_csv import import_csv_to_sqlite


TARGET_PROPERTIES = {
    "Glass transition temperature",
    "Melting temperature",
    "Thermal decomposition temperature",
    "Thermal decomposition weight loss",
    "Elongation at break",
    "Tensile stress strength at break",
    "O2 Permeability Barrer",
    "Co2 Permeability Barrer",
    "H2 Permeability Barrer",
}

CLEANED_FIELDS = [
    "smiles",
    "property_name",
    "property_value",
    "property_unit",
    "label_source",
]

CELSIUS_PROPERTIES = {
    "Glass transition temperature",
    "Melting temperature",
    "Thermal decomposition temperature",
}


def normalize_value_and_unit(property_name: str, value: str, unit: str) -> tuple[str, str]:
    normalized_unit = unit.strip()
    normalized_value = value.strip()

    if property_name not in CELSIUS_PROPERTIES:
        return normalized_value, normalized_unit

    if normalized_unit.lower() in {"c", "°c", "℃"}:
        return normalized_value, "C"

    if normalized_unit.lower() == "k":
        try:
            celsius_value = float(normalized_value) - 273.15
        except ValueError:
            return normalized_value, normalized_unit
        return f"{celsius_value:.10g}", "C"

    return normalized_value, normalized_unit


def clean_csv(source_path: Path, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0

    with source_path.open("r", encoding="utf-8-sig", newline="") as source_handle:
        reader = csv.DictReader(source_handle)
        with output_path.open("w", encoding="utf-8", newline="") as output_handle:
            writer = csv.DictWriter(output_handle, fieldnames=CLEANED_FIELDS)
            writer.writeheader()

            for row in reader:
                property_name = (row.get("property_name") or "").strip()
                if property_name not in TARGET_PROPERTIES:
                    continue

                property_value, property_unit = normalize_value_and_unit(
                    property_name,
                    row.get("property_value") or "",
                    row.get("property_unit") or "",
                )

                writer.writerow(
                    {
                        "smiles": (row.get("smiles") or "").strip(),
                        "property_name": property_name,
                        "property_value": property_value,
                        "property_unit": property_unit,
                        "label_source": (row.get("label_source") or "").strip(),
                    }
                )
                written += 1

    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare cleaned official PolyProp CSV and SQLite database.")
    parser.add_argument(
        "--source-csv",
        default="/mnt/d/database/polyprop/PolymerDatabase_head1000.csv",
        help="Raw PolyProp CSV path.",
    )
    parser.add_argument(
        "--cleaned-csv",
        default="/mnt/d/database/polyprop/polyprop_9_properties_clean.csv",
        help="Cleaned CSV output path.",
    )
    parser.add_argument(
        "--db-path",
        default="/mnt/d/database/polyprop/polyprop.db",
        help="SQLite database output path.",
    )
    args = parser.parse_args()

    source_csv = Path(args.source_csv)
    cleaned_csv = Path(args.cleaned_csv)
    db_path = Path(args.db_path)

    row_count = clean_csv(source_csv, cleaned_csv)
    stats = import_csv_to_sqlite(csv_path=cleaned_csv, db_path=db_path)

    print(f"Cleaned rows={row_count} csv={cleaned_csv}")
    print(
        f"Imported db={db_path} polymers={stats.polymer_count} properties={stats.property_count} "
        f"parse_ok={stats.parse_ok_count} parse_fail={stats.parse_fail_count}"
    )


if __name__ == "__main__":
    main()
