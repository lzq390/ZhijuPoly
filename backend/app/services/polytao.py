from __future__ import annotations

import math
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from rdkit import Chem
from rdkit.Chem import Descriptors, RDConfig

from app.models import POLYTAO_DESCRIPTOR_NAMES


POLYTAO_DEFAULT_CANDIDATE_COUNT = 10
POLYTAO_DEFAULT_TEMPERATURE = 1.0
POLYTAO_DEFAULT_TOP_K = 100
POLYTAO_DEFAULT_TOP_P = 0.999
POLYTAO_DEFAULT_MAX_LENGTH = 300


@dataclass(slots=True)
class PolytaoNormalizedCandidate:
    rank: int
    generated_smiles: str
    raw_smiles: str
    valid_smiles: bool = True
    sa_score: float | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PolytaoCandidateAccumulator:
    """Incrementally normalize decoder batches without reprocessing history."""

    requested_count: int
    seen: set[str] = field(default_factory=set)
    accepted: list[PolytaoNormalizedCandidate] = field(default_factory=list)
    filters: Counter[str] = field(default_factory=Counter)

    @property
    def complete(self) -> bool:
        return len(self.accepted) >= max(1, int(self.requested_count))

    def add(self, raw_candidates: Iterable[str]) -> None:
        for raw in raw_candidates:
            if self.complete:
                break
            raw_smiles = str(raw).strip()
            if not raw_smiles:
                self.filters["empty_raw_smiles"] += 1
                continue
            normalized = normalize_polytao_smiles(raw_smiles)
            if normalized is None:
                self.filters["rdkit_parse_failed"] += 1
                continue
            if count_attachment_points(normalized) < 2:
                self.filters["star_count_lt_2"] += 1
                continue
            if normalized in self.seen:
                self.filters["duplicate"] += 1
                continue
            self.seen.add(normalized)
            self.accepted.append(
                PolytaoNormalizedCandidate(
                    rank=len(self.accepted) + 1,
                    generated_smiles=normalized,
                    raw_smiles=raw_smiles,
                    sa_score=calculate_sa_score(normalized),
                )
            )

    def result(self) -> tuple[list[PolytaoNormalizedCandidate], dict[str, int]]:
        return list(self.accepted), dict(self.filters)


def canonicalize_smiles(smiles: str) -> str:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"invalid smiles: {smiles}")
    return Chem.MolToSmiles(mol, canonical=True)


def polytao_descriptor_values(smiles: str) -> tuple[str, dict[str, float]]:
    canonical_smiles = canonicalize_smiles(smiles)
    mol = Chem.MolFromSmiles(canonical_smiles)
    if mol is None:
        raise ValueError(f"invalid smiles: {smiles}")

    descriptor_map = dict(Descriptors.descList)
    values: dict[str, float] = {}
    for name in POLYTAO_DESCRIPTOR_NAMES:
        fn = descriptor_map.get(name)
        if fn is None:
            raise RuntimeError(f"RDKit descriptor is not available: {name}")
        values[name] = float(fn(mol))
    return canonical_smiles, values


def polytao_prompt_from_descriptors(descriptors: dict[str, float]) -> str:
    missing = [name for name in POLYTAO_DESCRIPTOR_NAMES if name not in descriptors]
    if missing:
        raise ValueError("missing PolyTAO descriptors: " + ", ".join(missing))
    return ",".join(_format_descriptor_value(descriptors[name]) for name in POLYTAO_DESCRIPTOR_NAMES)


def _format_descriptor_value(value: float) -> str:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("PolyTAO descriptor values must be finite")
    if abs(numeric - round(numeric)) < 1e-9:
        return str(int(round(numeric)))
    return f"{numeric:.6g}"


def normalize_polytao_smiles(smiles: str | None) -> str | None:
    if smiles is None:
        return None
    value = str(smiles).strip()
    if not value:
        return None
    value = value.replace("<pad>", "").replace("</s>", "").strip()
    value = value.replace("[*]", "__STAR__")
    value = value.replace("*", "[*]")
    value = value.replace("__STAR__", "[*]")
    mol = Chem.MolFromSmiles(value)
    if mol is None:
        return None
    return Chem.MolToSmiles(mol, canonical=True).replace("[*]", "*")


def count_attachment_points(smiles: str) -> int:
    normalized = normalize_polytao_smiles(smiles)
    if normalized is None:
        return 0
    mol = Chem.MolFromSmiles(normalized.replace("*", "[*]"))
    if mol is None:
        return 0
    return sum(1 for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0)


def calculate_sa_score(smiles: str) -> float | None:
    normalized = normalize_polytao_smiles(smiles)
    if normalized is None:
        return None
    mol = Chem.MolFromSmiles(normalized.replace("*", "[*]"))
    if mol is None:
        return None
    try:
        import sascorer  # type: ignore[import-not-found]

        return float(sascorer.calculateScore(mol))
    except Exception:
        try:
            import sys

            sys.path.append(os.path.join(RDConfig.RDContribDir, "SA_Score"))
            import sascorer  # type: ignore[import-not-found,no-redef]

            return float(sascorer.calculateScore(mol))
        except Exception:
            return None


def normalize_polytao_candidates(
    raw_candidates: list[str],
    requested_count: int,
) -> tuple[list[PolytaoNormalizedCandidate], dict[str, int]]:
    accumulator = PolytaoCandidateAccumulator(requested_count=requested_count)
    accumulator.add(raw_candidates)
    return accumulator.result()


def default_polytao_params() -> dict[str, float | int]:
    return {
        "candidate_count": POLYTAO_DEFAULT_CANDIDATE_COUNT,
        "temperature": POLYTAO_DEFAULT_TEMPERATURE,
        "top_k": POLYTAO_DEFAULT_TOP_K,
        "top_p": POLYTAO_DEFAULT_TOP_P,
        "max_length": POLYTAO_DEFAULT_MAX_LENGTH,
    }


def descriptor_response_items(descriptors: dict[str, float]) -> list[dict[str, Any]]:
    return [{"name": name, "value": descriptors[name]} for name in POLYTAO_DESCRIPTOR_NAMES]
