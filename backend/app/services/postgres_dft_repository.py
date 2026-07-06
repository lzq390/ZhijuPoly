from __future__ import annotations

from typing import Any


def count_dft_molecules_postgres(connection: Any) -> int:
    return int(connection.execute("SELECT COUNT(*) AS count FROM dft.molecule_final").fetchone()["count"])


def sample_pca_points_postgres(connection: Any, limit: int = 1000) -> list[dict[str, Any]]:
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
        FROM dft.molecule_final
        ORDER BY random()
        LIMIT %s
        """,
        (limit,),
    )
    return list(cursor.fetchall())


def get_molecule_final_postgres(connection: Any, mol_id: str) -> dict[str, Any] | None:
    cursor = connection.execute(
        """
        SELECT *
        FROM dft.molecule_final
        WHERE mol_id = %s
        """,
        (mol_id,),
    )
    return cursor.fetchone()


def get_energy_trace_postgres(connection: Any, mol_id: str) -> list[dict[str, Any]]:
    cursor = connection.execute(
        """
        SELECT step, scf_energy, homo_ev, lumo_ev, gap_ev
        FROM dft.energy_trace
        WHERE mol_id = %s
        ORDER BY step
        """,
        (mol_id,),
    )
    return list(cursor.fetchall())
