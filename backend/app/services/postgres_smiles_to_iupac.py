from __future__ import annotations

from typing import Any

from app.services.smiles_to_iupac import (
    IupacNameLookupAmbiguousError,
    IupacSmilesMatch,
    _cache_lookup_keys,
    _find_normalized_name,
    _has_name_boundaries,
    normalize_iupac_name,
)


def lookup_iupac_name_postgres(connection: Any, smiles: str) -> str | None:
    lookup_keys = _cache_lookup_keys(smiles)
    if not lookup_keys:
        return None

    for lookup_key in lookup_keys:
        row = connection.execute(
            """
            SELECT iupac_name
            FROM pi.monomer_iupac
            WHERE smiles = %s
            """,
            (lookup_key,),
        ).fetchone()
        if row is None:
            continue

        value = row["iupac_name"]
        if value is None:
            return None
        return str(value).strip() or None

    return None


def find_iupac_smiles_matches_postgres(connection: Any, text: str) -> list[IupacSmilesMatch]:
    normalized_text = normalize_iupac_name(text)
    if not normalized_text:
        return []

    candidates: list[tuple[int, int, IupacSmilesMatch]] = []
    for normalized_name, matches in _cached_iupac_match_groups_postgres(connection).items():
        position = _find_normalized_name(normalized_text, normalized_name)
        if position < 0:
            continue
        if len(matches) > 1:
            display_name = matches[0].iupac_name
            raise IupacNameLookupAmbiguousError(
                f"cached IUPAC name maps to multiple SMILES: {display_name}"
            )
        candidates.append((position, len(normalized_name), matches[0]))

    candidates.sort(key=lambda item: (-item[1], item[0]))
    selected: list[tuple[int, int, IupacSmilesMatch]] = []
    for position, length, match in candidates:
        span = (position, position + length)
        if any(
            _spans_overlap(span, (start, start + selected_length))
            for start, selected_length, _ in selected
        ):
            continue
        selected.append((position, length, match))

    selected.sort(key=lambda item: item[0])
    return [match for _, _, match in selected]


def _cached_iupac_match_groups_postgres(connection: Any) -> dict[str, list[IupacSmilesMatch]]:
    rows = connection.execute(
        """
        SELECT smiles, iupac_name
        FROM pi.monomer_iupac
        WHERE iupac_name IS NOT NULL
        """
    ).fetchall()

    grouped: dict[str, list[IupacSmilesMatch]] = {}
    seen_by_name: dict[str, set[str]] = {}
    for row in rows:
        smiles = str(row["smiles"] or "").strip()
        iupac_name = str(row["iupac_name"] or "").strip()
        normalized_name = normalize_iupac_name(iupac_name)
        if not smiles or not normalized_name:
            continue

        seen_smiles = seen_by_name.setdefault(normalized_name, set())
        if smiles in seen_smiles:
            continue

        seen_smiles.add(smiles)
        grouped.setdefault(normalized_name, []).append(
            IupacSmilesMatch(iupac_name=iupac_name, smiles=smiles)
        )

    return grouped


def _spans_overlap(first: tuple[int, int], second: tuple[int, int]) -> bool:
    return first[0] < second[1] and second[0] < first[1]
