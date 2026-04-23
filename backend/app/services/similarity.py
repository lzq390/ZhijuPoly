from __future__ import annotations

import sqlite3

from app.services.fingerprint import generate, tanimoto
from app.utils.exceptions import InvalidSmilesError


def similarity_search(
    connection: sqlite3.Connection,
    smiles: str,
    similarity_threshold: float = 0.7,
    top_k: int = 10,
) -> list[tuple[sqlite3.Row, float]]:
    try:
        query_fp = generate(smiles.strip())
    except ValueError as exc:
        raise InvalidSmilesError(str(exc)) from exc

    polymer_rows = connection.execute(
        """
        SELECT polymer_id, '' AS polymer_name, smiles, canonical_smiles, rdkit_parse_ok
        FROM polymers
        WHERE rdkit_parse_ok = 1
        ORDER BY polymer_id
        """
    ).fetchall()

    scored_rows: list[tuple[sqlite3.Row, float]] = []
    for row in polymer_rows:
        source_smiles = row["canonical_smiles"] or row["smiles"]

        try:
            candidate_fp = generate(source_smiles)
        except ValueError:
            continue

        score = tanimoto(query_fp, candidate_fp)
        if score >= similarity_threshold:
            scored_rows.append((row, score))

    scored_rows.sort(key=lambda item: (-item[1], int(item[0]["polymer_id"])))
    return scored_rows[:top_k]
