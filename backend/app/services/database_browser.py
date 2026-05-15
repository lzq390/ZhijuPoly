from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path


STRUCTURE_PROPERTY_SEARCH_COLUMNS = (
    "CAST(properties.property_id AS TEXT)",
    "CAST(properties.polymer_id AS TEXT)",
    "polymers.smiles",
    "COALESCE(polymers.canonical_smiles, '')",
    "properties.property_name",
    "properties.property_value",
    "COALESCE(properties.property_unit, '')",
    "COALESCE(properties.label_source, '')",
)

DFT_MOLECULE_SEARCH_COLUMNS = (
    "dft_molecule_final.mol_id",
    "dft_molecule_final.range_group",
    "CAST(dft_molecule_final.final_step AS TEXT)",
    "CAST(dft_molecule_final.n_atoms AS TEXT)",
    "CAST(dft_molecule_final.scf_energy AS TEXT)",
    "CAST(dft_molecule_final.homo_ev AS TEXT)",
    "CAST(dft_molecule_final.lumo_ev AS TEXT)",
    "CAST(dft_molecule_final.gap_ev AS TEXT)",
    "COALESCE(dft_molecule_final.is_converged, '')",
)

DFT_STEP_SEARCH_COLUMNS = (
    "dft_energy_trace.mol_id",
    "CAST(dft_energy_trace.step AS TEXT)",
    "CAST(dft_energy_trace.scf_energy AS TEXT)",
    "CAST(dft_energy_trace.homo_ev AS TEXT)",
    "CAST(dft_energy_trace.lumo_ev AS TEXT)",
    "CAST(dft_energy_trace.gap_ev AS TEXT)",
)


@dataclass(frozen=True)
class CsvBrowserRecord:
    source_file: str
    source_row_number: int
    data: dict[str, str]


def _smiles_match_params(query_smiles: str, canonical_smiles: str) -> list[str]:
    return [canonical_smiles, query_smiles, canonical_smiles]


def _smiles_match_where(polymer_alias: str = "polymers") -> str:
    return (
        f"{polymer_alias}.canonical_smiles = ? "
        f"OR {polymer_alias}.smiles = ? "
        f"OR {polymer_alias}.smiles = ?"
    )


def lookup_polymer_smiles(
    connection: sqlite3.Connection,
    *,
    query_smiles: str,
    canonical_smiles: str,
    limit: int = 25,
) -> tuple[int, list[sqlite3.Row]]:
    match_params = _smiles_match_params(query_smiles, canonical_smiles)
    where_sql = _smiles_match_where("polymers")
    total = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM polymers
            WHERE {where_sql}
            """,
            match_params,
        ).fetchone()[0]
    )

    rows = connection.execute(
        f"""
        SELECT
          polymers.polymer_id,
          polymers.smiles,
          polymers.canonical_smiles,
          polymers.rdkit_parse_ok,
          COUNT(properties.property_id) AS property_count,
          CASE
            WHEN polymers.canonical_smiles = ? THEN 'canonical_smiles'
            WHEN polymers.smiles = ? THEN 'smiles'
            WHEN polymers.smiles = ? THEN 'smiles'
            ELSE 'smiles'
          END AS source_column
        FROM polymers
        LEFT JOIN properties ON properties.polymer_id = polymers.polymer_id
        WHERE {where_sql}
        GROUP BY polymers.polymer_id
        ORDER BY polymers.polymer_id ASC
        LIMIT ?
        """,
        [*match_params, *match_params, limit],
    ).fetchall()

    return total, list(rows)


def lookup_property_smiles(
    connection: sqlite3.Connection,
    *,
    query_smiles: str,
    canonical_smiles: str,
    limit: int = 50,
) -> tuple[int, list[sqlite3.Row]]:
    match_params = _smiles_match_params(query_smiles, canonical_smiles)
    where_sql = _smiles_match_where("polymers")
    total = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM properties
            JOIN polymers ON polymers.polymer_id = properties.polymer_id
            WHERE {where_sql}
            """,
            match_params,
        ).fetchone()[0]
    )

    rows = connection.execute(
        f"""
        SELECT
          properties.property_id,
          properties.polymer_id,
          polymers.smiles,
          polymers.canonical_smiles,
          properties.property_name,
          properties.property_value,
          properties.property_value_num,
          properties.property_unit,
          properties.label_source,
          CASE
            WHEN polymers.canonical_smiles = ? THEN 'canonical_smiles'
            WHEN polymers.smiles = ? THEN 'smiles'
            WHEN polymers.smiles = ? THEN 'smiles'
            ELSE 'smiles'
          END AS source_column
        FROM properties
        JOIN polymers ON polymers.polymer_id = properties.polymer_id
        WHERE {where_sql}
        ORDER BY properties.property_id ASC
        LIMIT ?
        """,
        [*match_params, *match_params, limit],
    ).fetchall()

    return total, list(rows)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def lookup_pi_candidate_smiles(
    connection: sqlite3.Connection,
    *,
    query_smiles: str,
    canonical_smiles: str,
    limit: int = 50,
) -> tuple[int, list[sqlite3.Row]]:
    if not _table_exists(connection, "pi_candidates"):
        return 0, []

    source_params = [
        canonical_smiles,
        query_smiles,
        canonical_smiles,
        query_smiles,
        canonical_smiles,
        query_smiles,
        canonical_smiles,
    ]
    where_sql = """
      canonical_polym = ?
      OR polym = ?
      OR polym = ?
      OR mon1 = ?
      OR mon1 = ?
      OR mon2 = ?
      OR mon2 = ?
    """
    total = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM pi_candidates
            WHERE {where_sql}
            """,
            source_params,
        ).fetchone()[0]
    )

    rows = connection.execute(
        f"""
        SELECT
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
          CASE
            WHEN canonical_polym = ? THEN 'canonical_polym'
            WHEN polym = ? THEN 'polym'
            WHEN polym = ? THEN 'polym'
            WHEN mon1 = ? THEN 'mon1'
            WHEN mon1 = ? THEN 'mon1'
            WHEN mon2 = ? THEN 'mon2'
            WHEN mon2 = ? THEN 'mon2'
            ELSE 'polym'
          END AS source_column,
          CASE
            WHEN canonical_polym = ? THEN canonical_polym
            WHEN polym = ? THEN polym
            WHEN polym = ? THEN polym
            WHEN mon1 = ? THEN mon1
            WHEN mon1 = ? THEN mon1
            WHEN mon2 = ? THEN mon2
            WHEN mon2 = ? THEN mon2
            ELSE polym
          END AS matched_smiles
        FROM pi_candidates
        WHERE {where_sql}
        ORDER BY pi_id ASC
        LIMIT ?
        """,
        [*source_params, *source_params, *source_params, limit],
    ).fetchall()

    return total, list(rows)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _structure_property_where(query: str) -> tuple[str, list[str]]:
    return _like_where(query, STRUCTURE_PROPERTY_SEARCH_COLUMNS)


def _like_where(query: str, columns: tuple[str, ...]) -> tuple[str, list[str]]:
    normalized = query.strip()
    if not normalized:
        return "", []

    like_value = f"%{_escape_like(normalized)}%"
    parts = [f"{column} COLLATE NOCASE LIKE ? ESCAPE '\\'" for column in columns]
    return "WHERE " + " OR ".join(parts), [like_value] * len(parts)


def _dft_molecule_where(query: str) -> tuple[str, list[str]]:
    return _like_where(query, DFT_MOLECULE_SEARCH_COLUMNS)


def _dft_step_where(query: str) -> tuple[str, list[str]]:
    return _like_where(query, DFT_STEP_SEARCH_COLUMNS)


def browse_csv_records(
    csv_path: str | Path,
    *,
    source_file: str,
    query: str,
    page: int,
    page_size: int,
    search_fields: tuple[str, ...] | None = None,
) -> tuple[int, int, list[CsvBrowserRecord]]:
    normalized_query = query.strip().casefold()
    offset = (page - 1) * page_size
    total_records = 0
    matched_records = 0
    results: list[CsvBrowserRecord] = []

    with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = tuple(reader.fieldnames or ())
        fields = search_fields or fieldnames

        for source_row_number, row in enumerate(reader, start=2):
            total_records += 1
            if normalized_query and not any(normalized_query in (row.get(field, "") or "").casefold() for field in fields):
                continue

            matched_records += 1
            if matched_records <= offset or len(results) >= page_size:
                continue

            results.append(
                CsvBrowserRecord(
                    source_file=source_file,
                    source_row_number=source_row_number,
                    data={field: row.get(field, "") or "" for field in fieldnames},
                )
            )

    return total_records, matched_records, results


def browse_structure_property_records(
    connection: sqlite3.Connection,
    *,
    query: str,
    page: int,
    page_size: int,
) -> tuple[int, int, list[sqlite3.Row]]:
    total_records = int(connection.execute("SELECT COUNT(*) FROM properties").fetchone()[0])
    where_sql, params = _structure_property_where(query)
    offset = (page - 1) * page_size

    matched_records = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM properties
            JOIN polymers ON polymers.polymer_id = properties.polymer_id
            {where_sql}
            """,
            params,
        ).fetchone()[0]
    )

    rows = connection.execute(
        f"""
        SELECT
          properties.property_id,
          properties.polymer_id,
          polymers.smiles,
          polymers.canonical_smiles,
          properties.property_name,
          properties.property_value,
          properties.property_value_num,
          properties.property_unit,
          properties.label_source
        FROM properties
        JOIN polymers ON polymers.polymer_id = properties.polymer_id
        {where_sql}
        ORDER BY properties.property_id ASC
        LIMIT ? OFFSET ?
        """,
        [*params, page_size, offset],
    ).fetchall()

    return total_records, matched_records, list(rows)


def get_dft_browser_summary(connection: sqlite3.Connection) -> tuple[int, int, float, int]:
    molecule_count = int(connection.execute("SELECT COUNT(*) FROM dft_molecule_final").fetchone()[0])
    step_count = int(connection.execute("SELECT COUNT(*) FROM dft_energy_trace").fetchone()[0])
    stats_row = connection.execute(
        """
        SELECT AVG(final_step + 1) AS average_steps, MAX(final_step + 1) AS max_steps
        FROM dft_molecule_final
        """
    ).fetchone()
    average_steps = float(stats_row["average_steps"] or 0.0) if stats_row is not None else 0.0
    max_steps = int(stats_row["max_steps"] or 0) if stats_row is not None else 0
    return molecule_count, step_count, average_steps, max_steps


def browse_dft_molecules(
    connection: sqlite3.Connection,
    *,
    query: str,
    page: int,
    page_size: int,
) -> tuple[int, int, int, float, int, list[sqlite3.Row]]:
    total_records, total_step_records, average_steps, max_steps = get_dft_browser_summary(connection)
    where_sql, params = _dft_molecule_where(query)
    offset = (page - 1) * page_size

    matched_records = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM dft_molecule_final
            {where_sql}
            """,
            params,
        ).fetchone()[0]
    )

    rows = connection.execute(
        f"""
        SELECT
          dft_molecule_final.mol_id,
          dft_molecule_final.range_group,
          dft_molecule_final.final_step,
          dft_molecule_final.n_atoms,
          dft_molecule_final.final_step + 1 AS trace_points,
          dft_molecule_final.scf_energy,
          dft_molecule_final.zero_point_energy,
          dft_molecule_final.thermal_enthalpy,
          dft_molecule_final.gibbs_free_energy,
          dft_molecule_final.lowest_freq,
          dft_molecule_final.dipole_moment,
          dft_molecule_final.homo_ev,
          dft_molecule_final.lumo_ev,
          dft_molecule_final.gap_ev,
          dft_molecule_final.is_converged
        FROM dft_molecule_final
        {where_sql}
        ORDER BY dft_molecule_final.mol_id ASC
        LIMIT ? OFFSET ?
        """,
        [*params, page_size, offset],
    ).fetchall()

    return total_records, matched_records, total_step_records, average_steps, max_steps, list(rows)


def browse_dft_energy_steps(
    connection: sqlite3.Connection,
    *,
    query: str,
    mol_id: str | None = None,
    page: int,
    page_size: int,
) -> tuple[int, int, list[sqlite3.Row]]:
    total_records = int(connection.execute("SELECT COUNT(*) FROM dft_energy_trace").fetchone()[0])
    exact_mol_id = mol_id.strip() if mol_id is not None else ""
    if exact_mol_id:
        where_sql = "WHERE mol_id = ?"
        params = [exact_mol_id]
    else:
        where_sql, params = _dft_step_where(query)
    offset = (page - 1) * page_size

    matched_records = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM dft_energy_trace
            {where_sql}
            """,
            params,
        ).fetchone()[0]
    )

    rows = connection.execute(
        f"""
        SELECT
          mol_id,
          step,
          scf_energy,
          homo_ev,
          lumo_ev,
          gap_ev
        FROM dft_energy_trace
        {where_sql}
        ORDER BY mol_id ASC, step ASC
        LIMIT ? OFFSET ?
        """,
        [*params, page_size, offset],
    ).fetchall()

    return total_records, matched_records, list(rows)
