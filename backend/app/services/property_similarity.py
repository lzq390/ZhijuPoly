from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.predictor import PROPERTY_UNITS, predict


def property_similarity_search_postgres(
    connection: Any,
    smiles: str,
    property_name: str,
    model_dir: Path | None = None,
    similarity_threshold: float = 0.7,
    top_k: int = 10,
) -> tuple[float, str | None, list[tuple[Any, float]]]:
    target_value = predict(smiles, [property_name], model_dir=model_dir)[property_name]
    scale = max(abs(target_value), 1.0)
    distance_filter = ""
    params: list[object] = [target_value, property_name]
    if similarity_threshold > 0.0:
        max_distance = scale * ((1.0 / similarity_threshold) - 1.0)
        distance_filter = "AND ABS(pr.property_value_num - %s) <= %s"
        params.extend((target_value, max_distance))
    params.append(top_k)

    rows = connection.execute(
        f"""
        WITH nearest_per_polymer AS (
          SELECT DISTINCT ON (p.polymer_id)
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
              {distance_filter}
          ORDER BY
              p.polymer_id ASC,
              ABS(pr.property_value_num - %s) ASC,
              pr.property_id ASC
        )
        SELECT *
        FROM nearest_per_polymer
        ORDER BY property_distance ASC, polymer_id ASC
        LIMIT %s
        """,
        [*params[:-1], target_value, params[-1]],
    ).fetchall()

    rows_with_scores = [
        (row, 1.0 / (1.0 + float(row["property_distance"]) / scale))
        for row in rows
    ]
    return target_value, PROPERTY_UNITS.get(property_name), rows_with_scores
