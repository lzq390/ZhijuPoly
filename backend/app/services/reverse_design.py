from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from app.services.fingerprint import fingerprint_from_bytes, generate, tanimoto
from app.utils.exceptions import InvalidSmilesError

ProgressCallback = Callable[..., None]
CancellationCheck = Callable[[], bool]


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


def search_reverse_design_by_tg(
    connection: sqlite3.Connection,
    smiles: str,
    target_tg: float,
    *,
    similarity_threshold: float = 0.7,
    candidate_sample_size: int = 200,
    top_k: int = 50,
    random_seed: int | None = None,
    chunk_size: int = 5000,
    progress_callback: ProgressCallback | None = None,
    progress_interval_rows: int = 50000,
    cancellation_check: CancellationCheck | None = None,
) -> ReverseDesignSearchResult:
    try:
        query_fp = generate(smiles.strip())
    except ValueError as exc:
        raise InvalidSmilesError(str(exc)) from exc

    matches: list[ReverseDesignCandidate] = []
    candidate_pool_size = 0
    scanned_rows = 0
    best_similarity_score: float | None = None
    current_tg_radius: float | None = None
    progress_interval = max(1, progress_interval_rows)

    def emit_progress() -> None:
        if progress_callback is None:
            return
        progress_callback(
            scanned_rows=scanned_rows,
            matched_count=candidate_pool_size,
            current_tg_radius=current_tg_radius,
            best_similarity_score=best_similarity_score,
        )

    def build_result(*, exhausted: bool = False, cancelled: bool = False) -> ReverseDesignSearchResult:
        matches.sort(key=lambda item: (item.tg_difference, -item.similarity_score, item.pi_id))
        return ReverseDesignSearchResult(
            candidate_pool_size=candidate_pool_size,
            sampled_candidate_count=len(matches),
            results=matches[:top_k],
            scanned_rows=scanned_rows,
            best_similarity_score=best_similarity_score,
            current_tg_radius=current_tg_radius,
            exhausted=exhausted,
            cancelled=cancelled,
        )

    cursor = connection.execute(
        """
        SELECT
            pi_id,
            mon1,
            mon2,
            polym,
            canonical_polym,
            tg_celsius,
            morgan_fp
        FROM pi_candidates
        WHERE rdkit_parse_ok = 1
          AND morgan_fp IS NOT NULL
        ORDER BY pi_id
        """
    )

    while True:
        rows = cursor.fetchmany(chunk_size)
        if not rows:
            break

        for row in rows:
            scanned_rows += 1
            tg_value = float(row["tg_celsius"])
            current_tg_radius = abs(tg_value - float(target_tg))
            try:
                candidate_fp = fingerprint_from_bytes(row["morgan_fp"])
            except (TypeError, ValueError, RuntimeError):
                if scanned_rows % progress_interval == 0:
                    emit_progress()
                    if cancellation_check is not None and cancellation_check():
                        return build_result(cancelled=True)
                continue

            similarity_score = tanimoto(query_fp, candidate_fp)
            if best_similarity_score is None or similarity_score > best_similarity_score:
                best_similarity_score = similarity_score
            if similarity_score < similarity_threshold:
                if scanned_rows % progress_interval == 0:
                    emit_progress()
                    if cancellation_check is not None and cancellation_check():
                        return build_result(cancelled=True)
                continue

            candidate_pool_size += 1
            candidate = _build_candidate(row, similarity_score, float(target_tg))
            matches.append(candidate)
            if scanned_rows % progress_interval == 0:
                emit_progress()
                if cancellation_check is not None and cancellation_check():
                    return build_result(cancelled=True)

    return build_result(exhausted=True)
