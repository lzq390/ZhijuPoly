from __future__ import annotations

import ast
import json
import sqlite3
from typing import Any


def count_dft_molecules(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM dft_molecule_final").fetchone()[0])


def sample_pca_points(connection: sqlite3.Connection, limit: int = 1000) -> list[sqlite3.Row]:
    cursor = connection.execute(
        """
        SELECT
          mol_id,
          pca_x,
          pca_y,
          pca_z,
          n_atoms,
          final_step,
          homo_ev,
          lumo_ev,
          gap_ev,
          dipole_moment
        FROM dft_molecule_final
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (limit,),
    )
    return list(cursor.fetchall())


def get_molecule_final(connection: sqlite3.Connection, mol_id: str) -> sqlite3.Row | None:
    cursor = connection.execute(
        """
        SELECT *
        FROM dft_molecule_final
        WHERE mol_id = ?
        """,
        (mol_id,),
    )
    return cursor.fetchone()


def get_energy_trace(connection: sqlite3.Connection, mol_id: str) -> list[sqlite3.Row]:
    cursor = connection.execute(
        """
        SELECT step, scf_energy, homo_ev, lumo_ev, gap_ev
        FROM dft_energy_trace
        WHERE mol_id = ?
        ORDER BY step
        """,
        (mol_id,),
    )
    return list(cursor.fetchall())


def parse_coordinates(coordinates: str) -> list[tuple[int, float, float, float]]:
    try:
        parsed: Any = json.loads(coordinates)
    except json.JSONDecodeError:
        parsed = ast.literal_eval(coordinates)

    return [
        (int(atom[0]), float(atom[1]), float(atom[2]), float(atom[3]))
        for atom in parsed
    ]
