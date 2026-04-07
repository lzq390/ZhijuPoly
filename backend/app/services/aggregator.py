from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence

from app.models import PolymerResult, PropertyGroups, PropertyItem


CATEGORY_MAP = {
    "Thermal": "thermal",
    "Mechanical": "mechanical",
    "Electrical": "electrical",
    "Chemical": "chemical",
    "Optical": "optical",
    "Others": "other",
}


def fetch_property_rows(connection: sqlite3.Connection, polymer_id: int) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT
            property_category,
            property_name,
            property_value,
            property_value_num,
            property_unit,
            label_source
        FROM properties
        WHERE polymer_id = ?
        ORDER BY property_id
        """,
        (polymer_id,),
    ).fetchall()
    return list(rows)


def group_properties(property_rows: Iterable[sqlite3.Row]) -> PropertyGroups:
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


def build_polymer_result(
    polymer_row: sqlite3.Row,
    property_rows: Sequence[sqlite3.Row],
    similarity_score: float | None = None,
) -> PolymerResult:
    return PolymerResult(
        polymer_id=str(polymer_row["polymer_id"]),
        polymer_name=polymer_row["polymer_name"],
        smiles=polymer_row["smiles"],
        canonical_smiles=polymer_row["canonical_smiles"],
        similarity_score=similarity_score,
        properties=group_properties(property_rows),
    )


def load_polymer_result(
    connection: sqlite3.Connection,
    polymer_row: sqlite3.Row,
    similarity_score: float | None = None,
) -> PolymerResult:
    property_rows = fetch_property_rows(connection, int(polymer_row["polymer_id"]))
    return build_polymer_result(polymer_row, property_rows, similarity_score=similarity_score)


def load_polymer_results(
    connection: sqlite3.Connection,
    polymer_rows: Sequence[sqlite3.Row],
    similarity_scores: dict[int, float] | None = None,
) -> list[PolymerResult]:
    scores = similarity_scores or {}
    results: list[PolymerResult] = []
    for polymer_row in polymer_rows:
        polymer_id = int(polymer_row["polymer_id"])
        results.append(
            load_polymer_result(
                connection,
                polymer_row,
                similarity_score=scores.get(polymer_id),
            )
        )
    return results
