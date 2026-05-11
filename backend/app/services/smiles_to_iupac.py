from __future__ import annotations

import sqlite3

from app.pi_database import ensure_pi_schema


def lookup_iupac_name(connection: sqlite3.Connection, smiles: str) -> str | None:
    normalized = smiles.strip()
    if not normalized:
        return None

    ensure_pi_schema(connection)
    row = connection.execute(
        """
        SELECT iupac_name
        FROM smiles_iupac_cache
        WHERE smiles = ?
        """,
        (normalized,),
    ).fetchone()
    if row is None:
        return None

    value = row["iupac_name"]
    if value is None:
        return None
    return str(value).strip() or None


def cache_iupac_name(connection: sqlite3.Connection, smiles: str, iupac_name: str | None) -> None:
    normalized_smiles = smiles.strip()
    if not normalized_smiles:
        return

    ensure_pi_schema(connection)
    connection.execute(
        """
        INSERT OR REPLACE INTO smiles_iupac_cache (smiles, iupac_name)
        VALUES (?, ?)
        """,
        (normalized_smiles, iupac_name.strip() if iupac_name else None),
    )
