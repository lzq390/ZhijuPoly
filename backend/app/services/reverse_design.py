from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ReverseDesignCandidate:
    pi_id: int
    polymer_smiles: str
    canonical_polym: str | None
    monomer_a_smiles: str
    monomer_b_smiles: str
    tg_value: float
    tg_difference: float
    similarity_score: float
    monomer_a_iupac: str | None = None
    monomer_b_iupac: str | None = None


@dataclass(slots=True)
class ReverseDesignSearchResult:
    candidate_pool_size: int
    sampled_candidate_count: int
    results: list[ReverseDesignCandidate]
    scanned_rows: int = 0
    best_similarity_score: float | None = None
    current_tg_radius: float | None = None
    exhausted: bool = False
    stopped_by_limit: bool = False
    cancelled: bool = False


def _row_get(row: Any, key: str, default: object = None) -> object:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_candidate(row: Any, similarity_score: float, target_tg: float) -> ReverseDesignCandidate:
    tg_value = float(_row_get(row, "tg_celsius"))
    return ReverseDesignCandidate(
        pi_id=int(_row_get(row, "pi_id")),
        polymer_smiles=str(_row_get(row, "polym", "")),
        canonical_polym=_optional_text(_row_get(row, "canonical_polym")),
        monomer_a_smiles=str(_row_get(row, "mon1", "")),
        monomer_b_smiles=str(_row_get(row, "mon2", "")),
        monomer_a_iupac=_optional_text(_row_get(row, "mon1_iupac_name")),
        monomer_b_iupac=_optional_text(_row_get(row, "mon2_iupac_name")),
        tg_value=tg_value,
        tg_difference=abs(tg_value - target_tg),
        similarity_score=similarity_score,
    )