from __future__ import annotations

import sqlite3

from app.services.smiles_utils import normalize
from app.utils.exceptions import InvalidSmilesError


def exact_match(connection: sqlite3.Connection, smiles: str) -> list[sqlite3.Row]:
    raw_smiles = smiles.strip()
    try:
        canonical_smiles = normalize(raw_smiles)
    except ValueError as exc:
        raise InvalidSmilesError(str(exc)) from exc

    canonical_rows = connection.execute(
        """
        SELECT polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok
        FROM polymers
        WHERE canonical_smiles = ?
        ORDER BY polymer_id
        """,
        (canonical_smiles,),
    ).fetchall()
    if canonical_rows:
        return list(canonical_rows)

    raw_rows = connection.execute(
        """
        SELECT polymer_id, polymer_name, smiles, canonical_smiles, rdkit_parse_ok
        FROM polymers
        WHERE smiles = ?
        ORDER BY polymer_id
        """,
        (raw_smiles,),
    ).fetchall()
    return list(raw_rows)
