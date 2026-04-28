from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.config import get_settings
from app.database import rebuild_fumol_schema, sqlite_connection


CSV_NAME = "FuMolDatabase.csv"
ELEMENTS = [1, 5, 6, 7, 8, 9, 14, 15, 16, 17]
NUMERIC_FEATURE_COLUMNS = [
    "n_atoms",
    "scf_energy",
    "zero_point_energy",
    "thermal_enthalpy",
    "gibbs_free_energy",
    "lowest_freq",
    "dipole_moment",
    "homo_ev",
    "lumo_ev",
    "gap_ev",
]
FINAL_COLUMNS = [
    "range_group",
    "mol_id",
    "step",
    "coordinates",
    "n_atoms",
    "scf_energy",
    "zero_point_energy",
    "thermal_enthalpy",
    "gibbs_free_energy",
    "lowest_freq",
    "dipole_moment",
    "homo_ev",
    "lumo_ev",
    "gap_ev",
    "is_converged",
]


@dataclass(frozen=True)
class FumolImportStats:
    source_rows: int
    molecule_count: int
    trace_count: int
    final_count: int


def parse_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: object, default: int = 0) -> int:
    parsed = parse_float(value)
    return default if parsed is None else int(parsed)


def parse_coordinates(value: str) -> list[list[float]]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = ast.literal_eval(value)
    return parsed


def geometry_features(coordinates: list[list[float]]) -> list[float]:
    positions = np.array([[atom[1], atom[2], atom[3]] for atom in coordinates], dtype=float)
    centered = positions - positions.mean(axis=0)
    radii = np.linalg.norm(centered, axis=1)
    extents = positions.max(axis=0) - positions.min(axis=0)
    covariance = np.cov(centered.T) if len(positions) > 1 else np.zeros((3, 3))
    eigenvalues = np.sort(np.linalg.eigvalsh(covariance))[::-1]

    return [
        float(radii.mean()),
        float(radii.std()),
        float(radii.max()),
        float(extents[0]),
        float(extents[1]),
        float(extents[2]),
        float(eigenvalues[0]),
        float(eigenvalues[1]),
        float(eigenvalues[2]),
    ]


def final_feature_vector(row: dict[str, str]) -> list[float]:
    coordinates = parse_coordinates(row["coordinates"])
    atom_counts = {element: 0 for element in ELEMENTS}
    for atom in coordinates:
        atom_counts[int(atom[0])] = atom_counts.get(int(atom[0]), 0) + 1

    n_atoms = max(parse_int(row["n_atoms"], len(coordinates)), 1)
    counts = [float(atom_counts[element]) for element in ELEMENTS]
    ratios = [count / n_atoms for count in counts]
    numeric = [parse_float(row[column]) for column in NUMERIC_FEATURE_COLUMNS]
    numeric_features = [np.nan if value is None else value for value in numeric]

    return counts + ratios + numeric_features + geometry_features(coordinates)


def pca_scores(feature_rows: list[list[float]]) -> np.ndarray:
    matrix = np.array(feature_rows, dtype=float)
    medians = np.nanmedian(matrix, axis=0)
    medians = np.where(np.isnan(medians), 0.0, medians)
    missing = np.where(np.isnan(matrix))
    matrix[missing] = np.take(medians, missing[1])

    means = matrix.mean(axis=0)
    stds = matrix.std(axis=0)
    stds = np.where(stds == 0, 1.0, stds)
    normalized = (matrix - means) / stds

    _, _, vt = np.linalg.svd(normalized, full_matrices=False)
    return normalized @ vt[:3].T


def import_fumol_to_sqlite(zip_path: str | Path, db_path: str | Path) -> FumolImportStats:
    zip_path = Path(zip_path)
    db_path = Path(db_path)
    latest: dict[str, tuple[int, dict[str, str]]] = {}
    trace_batch: list[tuple[Any, ...]] = []
    source_rows = 0
    trace_count = 0

    with sqlite_connection(db_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL;")
        connection.execute("PRAGMA synchronous = OFF;")
        rebuild_fumol_schema(connection)

        with zipfile.ZipFile(zip_path) as archive:
            with archive.open(CSV_NAME) as raw:
                text_stream = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                reader = csv.DictReader(text_stream)
                for row in reader:
                    source_rows += 1
                    mol_id = row["mol_id"]
                    step = parse_int(row["step"], -1)

                    scf_energy = parse_float(row.get("scf_energy"))
                    homo_ev = parse_float(row.get("homo_ev"))
                    lumo_ev = parse_float(row.get("lumo_ev"))
                    gap_ev = parse_float(row.get("gap_ev"))
                    if any(value is not None for value in (scf_energy, homo_ev, lumo_ev, gap_ev)):
                        trace_batch.append((mol_id, step, scf_energy, homo_ev, lumo_ev, gap_ev))

                    previous = latest.get(mol_id)
                    if previous is None or step > previous[0]:
                        latest[mol_id] = (step, {column: row.get(column, "") for column in FINAL_COLUMNS})

                    if len(trace_batch) >= 10000:
                        connection.executemany(
                            """
                            INSERT OR REPLACE INTO dft_energy_trace (
                              mol_id, step, scf_energy, homo_ev, lumo_ev, gap_ev
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            trace_batch,
                        )
                        trace_count += len(trace_batch)
                        trace_batch.clear()

        if trace_batch:
            connection.executemany(
                """
                INSERT OR REPLACE INTO dft_energy_trace (
                  mol_id, step, scf_energy, homo_ev, lumo_ev, gap_ev
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                trace_batch,
            )
            trace_count += len(trace_batch)

        final_rows = [row for _, row in latest.values()]
        features = [final_feature_vector(row) for row in final_rows]
        scores = pca_scores(features)

        final_values = []
        for row, score in zip(final_rows, scores, strict=True):
            final_values.append(
                (
                    row["mol_id"],
                    row["range_group"],
                    parse_int(row["step"]),
                    parse_int(row["n_atoms"]),
                    row["coordinates"],
                    parse_float(row["scf_energy"]),
                    parse_float(row["zero_point_energy"]),
                    parse_float(row["thermal_enthalpy"]),
                    parse_float(row["gibbs_free_energy"]),
                    parse_float(row["lowest_freq"]),
                    parse_float(row["dipole_moment"]),
                    parse_float(row["homo_ev"]),
                    parse_float(row["lumo_ev"]),
                    parse_float(row["gap_ev"]),
                    row["is_converged"].strip() or None,
                    float(score[0]),
                    float(score[1]),
                    float(score[2]),
                )
            )

        connection.executemany(
            """
            INSERT INTO dft_molecule_final (
              mol_id, range_group, final_step, n_atoms, coordinates, scf_energy,
              zero_point_energy, thermal_enthalpy, gibbs_free_energy, lowest_freq,
              dipole_moment, homo_ev, lumo_ev, gap_ev, is_converged, pca_x, pca_y, pca_z
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            final_values,
        )

    return FumolImportStats(
        source_rows=source_rows,
        molecule_count=len(latest),
        trace_count=trace_count,
        final_count=len(final_values),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import FuMol DFT data and precompute PCA coordinates.")
    parser.add_argument("--zip-path", default=None, help="Path to FuMolDatabase.zip")
    parser.add_argument("--db-path", default=None, help="Output SQLite database path")
    args = parser.parse_args()

    settings = get_settings()
    stats = import_fumol_to_sqlite(
        zip_path=args.zip_path or settings.fumol_zip_file,
        db_path=args.db_path or settings.fumol_db_file,
    )
    print(
        "Imported FuMol: "
        f"{stats.source_rows} rows, {stats.molecule_count} molecule groups, "
        f"{stats.trace_count} trace rows, {stats.final_count} final rows."
    )


if __name__ == "__main__":
    main()
