from __future__ import annotations

from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Any

from app.models import PolymerResult, PropertyGroups, PropertyItem
from app.services.structure_2d import generate_2d_svg


CATEGORY_MAP = {
    "Thermal": "thermal",
    "Mechanical": "mechanical",
    "Electrical": "electrical",
    "Chemical": "chemical",
    "Optical": "optical",
    "Others": "other",
}


@lru_cache(maxsize=4096)
def _generate_2d_svg_cached(smiles: str) -> str | None:
    """Reuse deterministic depictions across warm similarity queries."""

    return generate_2d_svg(smiles)


def fetch_property_rows_postgres(connection: Any, polymer_id: int) -> list[Any]:
    rows = connection.execute(
        """
        SELECT
            property_category,
            property_name,
            property_value,
            property_value_num,
            property_unit,
            label_source
        FROM core.polymer_properties
        WHERE polymer_id = %s
        ORDER BY property_id
        """,
        (polymer_id,),
    ).fetchall()
    return list(rows)


def fetch_property_rows_for_polymers_postgres(
    connection: Any,
    polymer_ids: Sequence[int],
) -> dict[int, list[Any]]:
    if not polymer_ids:
        return {}

    rows = connection.execute(
        """
        SELECT
            polymer_id,
            property_category,
            property_name,
            property_value,
            property_value_num,
            property_unit,
            label_source
        FROM core.polymer_properties
        WHERE polymer_id = ANY(%s)
        ORDER BY polymer_id, property_id
        """,
        (list(polymer_ids),),
    ).fetchall()
    grouped = {polymer_id: [] for polymer_id in polymer_ids}
    for row in rows:
        grouped.setdefault(int(row["polymer_id"]), []).append(row)
    return grouped


def group_properties(property_rows: Iterable[Any]) -> PropertyGroups:
    grouped: dict[str, list[PropertyItem]] = {
        "thermal": [],
        "mechanical": [],
        "electrical": [],
        "chemical": [],
        "optical": [],
        "other": [],
    }

    for row in property_rows:
        group_name = CATEGORY_MAP.get(row["property_category"], "other")
        grouped[group_name].append(
            PropertyItem(
                property_category=row["property_category"],
                property_name=row["property_name"],
                property_value=row["property_value"],
                property_value_num=row["property_value_num"],
                property_unit=row["property_unit"],
                label_source=row["label_source"],
            )
        )

    return PropertyGroups(**grouped)


def _row_keys(row: Any) -> set[str]:
    keys = row.keys() if hasattr(row, "keys") else []
    return set(keys)


def build_polymer_result(
    polymer_row: Any,
    property_rows: Sequence[Any],
    similarity_score: float | None = None,
) -> PolymerResult:
    polymer_keys = _row_keys(polymer_row)
    source_smiles = polymer_row["canonical_smiles"] or polymer_row["smiles"]
    polymer_name = polymer_row["polymer_name"] if "polymer_name" in polymer_keys else ""

    return PolymerResult(
        polymer_id=str(polymer_row["polymer_id"]),
        polymer_name=polymer_name or "",
        smiles=polymer_row["smiles"],
        canonical_smiles=polymer_row["canonical_smiles"],
        similarity_score=similarity_score,
        structure_svg=_generate_2d_svg_cached(source_smiles),
        matched_property_name=polymer_row["matched_property_name"] if "matched_property_name" in polymer_keys else None,
        matched_property_value=polymer_row["matched_property_value"] if "matched_property_value" in polymer_keys else None,
        matched_property_unit=polymer_row["matched_property_unit"] if "matched_property_unit" in polymer_keys else None,
        matched_property_source=polymer_row["matched_property_source"] if "matched_property_source" in polymer_keys else None,
        properties=group_properties(property_rows),
    )


def load_polymer_result_postgres(
    connection: Any,
    polymer_row: Any,
    similarity_score: float | None = None,
) -> PolymerResult:
    property_rows = fetch_property_rows_postgres(connection, int(polymer_row["polymer_id"]))
    return build_polymer_result(polymer_row, property_rows, similarity_score=similarity_score)


def load_polymer_results_postgres(
    connection: Any,
    polymer_rows: Sequence[Any],
    similarity_scores: dict[int, float] | None = None,
) -> list[PolymerResult]:
    scores = similarity_scores or {}
    polymer_ids = [int(polymer_row["polymer_id"]) for polymer_row in polymer_rows]
    property_rows_by_polymer = fetch_property_rows_for_polymers_postgres(
        connection,
        polymer_ids,
    )
    return [
        build_polymer_result(
            polymer_row,
            property_rows_by_polymer.get(int(polymer_row["polymer_id"]), []),
            similarity_score=scores.get(int(polymer_row["polymer_id"])),
        )
        for polymer_row in polymer_rows
    ]
