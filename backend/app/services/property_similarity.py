from __future__ import annotations

import sqlite3
from pathlib import Path

from app.services.predictor import PROPERTY_UNITS, predict


def property_similarity_search(
    connection: sqlite3.Connection,
    smiles: str,
    property_name: str,
    model_dir: Path | None = None,
    top_k: int = 10,
) -> tuple[float, str | None, list[tuple[sqlite3.Row, float]]]:
    target_value = predict(smiles, [property_name], model_dir=model_dir)[property_name]

    rows = connection.execute(
        """
        SELECT
            p.polymer_id,
            '' AS polymer_name,
            p.smiles,
            p.canonical_smiles,
            p.rdkit_parse_ok,
            pr.property_name AS matched_property_name,
            pr.property_value_num AS matched_property_value,
            pr.property_unit AS matched_property_unit,
            pr.label_source AS matched_property_source,
            ABS(pr.property_value_num - ?) AS property_distance
        FROM polymers AS p
        JOIN properties AS pr ON pr.polymer_id = p.polymer_id
        WHERE
            p.rdkit_parse_ok = 1
            AND pr.property_name = ?
            AND pr.property_value_num IS NOT NULL
        ORDER BY property_distance ASC, p.polymer_id ASC
        LIMIT ?
        """,
        (target_value, property_name, top_k),
    ).fetchall()

    scale = max(abs(target_value), 1.0)
    rows_with_scores = [
        (row, 1.0 / (1.0 + float(row["property_distance"]) / scale))
        for row in rows
    ]
    return target_value, PROPERTY_UNITS.get(property_name), rows_with_scores
