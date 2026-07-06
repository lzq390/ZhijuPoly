from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.predictor import PROPERTY_UNITS, predict


def property_similarity_search_postgres(
    connection: Any,
    smiles: str,
    property_name: str,
    model_dir: Path | None = None,
    top_k: int = 10,
) -> tuple[float, str | None, list[tuple[Any, float]]]:
    target_value = predict(smiles, [property_name], model_dir=model_dir)[property_name]

    rows = connection.execute(
        """
        SELECT
            p.polymer_id,
            p.polymer_name,
            p.smiles,
            p.canonical_smiles,
            p.rdkit_parse_ok,
            pr.property_name AS matched_property_name,
            pr.property_value_num AS matched_property_value,
            pr.property_unit AS matched_property_unit,
            pr.label_source AS matched_property_source,
            ABS(pr.property_value_num - %s) AS property_distance
        FROM core.polymers AS p
        JOIN core.polymer_properties AS pr ON pr.polymer_id = p.polymer_id
        WHERE
            p.rdkit_parse_ok = true
            AND pr.property_name = %s
            AND pr.property_value_num IS NOT NULL
        ORDER BY property_distance ASC, p.polymer_id ASC
        LIMIT %s
        """,
        (target_value, property_name, top_k),
    ).fetchall()

    scale = max(abs(target_value), 1.0)
    rows_with_scores = [
        (row, 1.0 / (1.0 + float(row["property_distance"]) / scale))
        for row in rows
    ]
    return target_value, PROPERTY_UNITS.get(property_name), rows_with_scores