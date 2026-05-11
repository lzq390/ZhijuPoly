from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.config import get_settings
from app.database import sqlite_connection
from app.pi_database import ensure_pi_schema, rebuild_pi_schema
from app.services.fingerprint import fingerprint_to_bytes, generate
from app.services.smiles_utils import normalize


OPTIONAL_FLOAT_FIELDS = (
    "dielectric_const_dc",
    "static_dielectric_const",
    "dipole_debye",
    "electrophilicity_index",
    "homo_lumo_gap_ev",
    "hardness",
    "mulliken_electronegativity",
    "redox_window_v",
    "linear_expansion",
    "refractive_index",
)

REQUIRED_COLUMNS = {"id", "mon1", "mon2", "polym", "tg_celsius"}

INSERT_SQL = """
INSERT OR REPLACE INTO pi_candidates (
    pi_id,
    mon1,
    mon2,
    polym,
    canonical_polym,
    rdkit_parse_ok,
    tg_celsius,
    dielectric_const_dc,
    static_dielectric_const,
    dipole_debye,
    electrophilicity_index,
    homo_lumo_gap_ev,
    hardness,
    mulliken_electronegativity,
    redox_window_v,
    linear_expansion,
    refractive_index,
    morgan_fp,
    created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


@dataclass(slots=True)
class PiCandidateImportStats:
    total_rows: int
    imported_count: int
    parse_ok_count: int
    parse_fail_count: int
    missing_tg_count: int
    skipped_required_count: int


def _parse_float(value: object) -> float | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        return float(text)
    except ValueError:
        return None


def _parse_pi_id(value: object) -> int | None:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        return int(text)
    except ValueError:
        return None


def _validate_header(fieldnames: Iterable[str] | None) -> None:
    columns = {field.strip() for field in fieldnames or []}
    missing = sorted(REQUIRED_COLUMNS - columns)
    if missing:
        raise ValueError(f"PI candidate CSV is missing required columns: {', '.join(missing)}")


def _build_insert_values(row: dict[str, str]) -> tuple[object, ...] | None:
    pi_id = _parse_pi_id(row.get("id"))
    if pi_id is None:
        return None

    tg_celsius = _parse_float(row.get("tg_celsius"))
    if tg_celsius is None:
        return None

    polym = (row.get("polym") or "").strip()
    if not polym:
        return None

    canonical_polym: str | None = None
    morgan_fp: bytes | None = None
    rdkit_parse_ok = 0

    try:
        canonical_polym = normalize(polym)
        morgan_fp = fingerprint_to_bytes(generate(canonical_polym))
        rdkit_parse_ok = 1
    except ValueError:
        pass

    return (
        pi_id,
        (row.get("mon1") or "").strip(),
        (row.get("mon2") or "").strip(),
        polym,
        canonical_polym,
        rdkit_parse_ok,
        tg_celsius,
        *(_parse_float(row.get(field)) for field in OPTIONAL_FLOAT_FIELDS),
        morgan_fp,
        (row.get("created_at") or "").strip() or None,
    )


def import_pi_candidates_to_sqlite(
    csv_path: str | Path,
    db_path: str | Path,
    *,
    rebuild: bool = True,
    limit: int | None = None,
    batch_size: int = 5000,
    progress_interval: int = 100000,
) -> PiCandidateImportStats:
    total_rows = 0
    imported_count = 0
    parse_ok_count = 0
    parse_fail_count = 0
    missing_tg_count = 0
    skipped_required_count = 0
    batch: list[tuple[object, ...]] = []

    with sqlite_connection(db_path) as connection:
        if rebuild:
            rebuild_pi_schema(connection)
        else:
            ensure_pi_schema(connection)

        with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            _validate_header(reader.fieldnames)

            for row in reader:
                if limit is not None and total_rows >= limit:
                    break

                total_rows += 1
                if _parse_float(row.get("tg_celsius")) is None:
                    missing_tg_count += 1

                values = _build_insert_values(row)
                if values is None:
                    skipped_required_count += 1
                    continue

                if values[5] == 1:
                    parse_ok_count += 1
                else:
                    parse_fail_count += 1

                batch.append(values)
                imported_count += 1

                if len(batch) >= batch_size:
                    connection.executemany(INSERT_SQL, batch)
                    batch.clear()

                if progress_interval > 0 and total_rows % progress_interval == 0:
                    print(
                        "Processed "
                        f"rows={total_rows} imported={imported_count} "
                        f"parse_ok={parse_ok_count} parse_fail={parse_fail_count}"
                    )

        if batch:
            connection.executemany(INSERT_SQL, batch)

    return PiCandidateImportStats(
        total_rows=total_rows,
        imported_count=imported_count,
        parse_ok_count=parse_ok_count,
        parse_fail_count=parse_fail_count,
        missing_tg_count=missing_tg_count,
        skipped_required_count=skipped_required_count,
    )


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Import PI reverse-design candidates into local SQLite.")
    parser.add_argument("--csv", "--csv-path", dest="csv_path", default=None)
    parser.add_argument("--db", "--db-path", dest="db_path", default=str(settings.pi_reverse_db_file))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--progress-interval", type=int, default=100000)
    parser.add_argument("--append", action="store_true", help="Append to the existing PI candidate database.")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the PI candidate database before import.")
    args = parser.parse_args()

    csv_path = args.csv_path or settings.pi_reverse_csv_file
    if csv_path is None:
        raise SystemExit("PI CSV path is required. Use --csv or PI_REVERSE_CSV_PATH.")

    stats = import_pi_candidates_to_sqlite(
        csv_path=csv_path,
        db_path=args.db_path,
        rebuild=True if args.rebuild or not args.append else False,
        limit=args.limit,
        batch_size=args.batch_size,
        progress_interval=args.progress_interval,
    )
    print(
        f"Imported pi_candidates={stats.imported_count} rows={stats.total_rows}"
        f" parse_ok={stats.parse_ok_count} parse_fail={stats.parse_fail_count}"
        f" missing_tg={stats.missing_tg_count} skipped={stats.skipped_required_count}"
    )


if __name__ == "__main__":
    main()
