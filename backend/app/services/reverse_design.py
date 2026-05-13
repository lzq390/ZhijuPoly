from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass

from app.services.fingerprint import fingerprint_from_bytes, generate, tanimoto
from app.utils.exceptions import InvalidSmilesError


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


@dataclass(slots=True)
class ReverseDesignSearchResult:
    candidate_pool_size: int
    sampled_candidate_count: int
    results: list[ReverseDesignCandidate]


def _build_candidate(row: sqlite3.Row, similarity_score: float, target_tg: float) -> ReverseDesignCandidate:
    tg_value = float(row["tg_celsius"])
    return ReverseDesignCandidate(
        pi_id=int(row["pi_id"]),
        polymer_smiles=row["polym"],
        canonical_polym=row["canonical_polym"],
        monomer_a_smiles=row["mon1"],
        monomer_b_smiles=row["mon2"],
        tg_value=tg_value,
        tg_difference=abs(tg_value - target_tg),
        similarity_score=similarity_score,
    )


def _add_to_reservoir(
    sample: list[ReverseDesignCandidate],
    candidate: ReverseDesignCandidate,
    *,
    seen_count: int,
    sample_size: int,
    rng: random.Random,
) -> None:
    if len(sample) < sample_size:
        sample.append(candidate)
        return

    replacement_index = rng.randrange(seen_count)
    if replacement_index < sample_size:
        sample[replacement_index] = candidate


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
) -> ReverseDesignSearchResult:
    try:
        query_fp = generate(smiles.strip())
    except ValueError as exc:
        raise InvalidSmilesError(str(exc)) from exc

    rng = random.Random(random_seed)
    sample: list[ReverseDesignCandidate] = []
    candidate_pool_size = 0

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
            try:
                candidate_fp = fingerprint_from_bytes(row["morgan_fp"])
            except (TypeError, ValueError, RuntimeError):
                continue

            similarity_score = tanimoto(query_fp, candidate_fp)
            if similarity_score < similarity_threshold:
                continue

            candidate_pool_size += 1
            candidate = _build_candidate(row, similarity_score, float(target_tg))
            _add_to_reservoir(
                sample,
                candidate,
                seen_count=candidate_pool_size,
                sample_size=candidate_sample_size,
                rng=rng,
            )

    sample.sort(key=lambda item: (item.tg_difference, -item.similarity_score, item.pi_id))
    return ReverseDesignSearchResult(
        candidate_pool_size=candidate_pool_size,
        sampled_candidate_count=len(sample),
        results=sample[:top_k],
    )
