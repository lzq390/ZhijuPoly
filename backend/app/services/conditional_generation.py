from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

from rdkit import Chem, DataStructs
from rdkit.Chem import AllChem, RDConfig

from app.utils.exceptions import ModelArtifactError


ALLOWED_ELEMENTS = {"H", "C", "N", "O", "F", "S", "Cl", "*"}
MAX_HEAVY_ATOMS = 30
DEFAULT_MAX_SMILES_LEN = 128
@dataclass(slots=True)
class GeneratedSmiles:
    raw_smiles: str
    rdkit_smiles: str | None


@dataclass(slots=True)
class ConditionalGenerationCandidate:
    rank: int
    generated_smiles: str
    predicted_tg: float | None
    tg_error: float | None
    similarity_score: float | None
    sa_score: float | None


@dataclass(slots=True)
class ConditionalGenerationResult:
    input_smiles_model: str
    input_smiles_rdkit: str
    delta_tg: float
    requested_count: int
    attempts: int
    filter_counter: dict[str, int]
    candidates: list[ConditionalGenerationCandidate] = field(default_factory=list)


class ConditionalGenerationRuntime(Protocol):
    def generate_once(
        self,
        *,
        input_smiles: str,
        delta_tg: float,
        top_k: int,
        temperature: float,
        max_length: int,
    ) -> GeneratedSmiles:
        ...

    def predict_tg(self, smiles: str) -> float:
        ...


def to_model_smiles(smiles: str | None) -> str | None:
    if smiles is None:
        return None

    value = str(smiles).strip()
    if not value:
        return None

    rdkit_smiles = to_rdkit_smiles(value)
    if rdkit_smiles is None:
        return None

    return rdkit_smiles.replace("[*]", "*")


def to_rdkit_smiles(smiles: str | None) -> str | None:
    if smiles is None:
        return None

    value = str(smiles).strip()
    if not value:
        return None

    value = value.replace("[*]", "__STAR__")
    value = value.replace("*", "[*]")
    value = value.replace("__STAR__", "[*]")

    mol = Chem.MolFromSmiles(value)
    if mol is None:
        return None

    return Chem.MolToSmiles(mol, canonical=True)


def count_attachment_points(smiles: str) -> int:
    rdkit_smiles = to_rdkit_smiles(smiles)
    if rdkit_smiles is None:
        return 0

    mol = Chem.MolFromSmiles(rdkit_smiles)
    if mol is None:
        return 0

    return sum(1 for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0)


def is_valid_polymer(smiles: str) -> bool:
    rdkit_smiles = to_rdkit_smiles(smiles)
    if rdkit_smiles is None:
        return False

    mol = Chem.MolFromSmiles(rdkit_smiles)
    if mol is None or mol.GetNumHeavyAtoms() > MAX_HEAVY_ATOMS:
        return False

    for atom in mol.GetAtoms():
        symbol = "*" if atom.GetAtomicNum() == 0 else atom.GetSymbol()
        if symbol not in ALLOWED_ELEMENTS:
            return False

    return count_attachment_points(rdkit_smiles) >= 2


def calculate_similarity(smiles_a: str, smiles_b: str) -> float | None:
    rdkit_a = to_rdkit_smiles(smiles_a)
    rdkit_b = to_rdkit_smiles(smiles_b)
    if rdkit_a is None or rdkit_b is None:
        return None

    mol_a = Chem.MolFromSmiles(rdkit_a)
    mol_b = Chem.MolFromSmiles(rdkit_b)
    if mol_a is None or mol_b is None:
        return None

    fp_a = AllChem.GetMorganFingerprintAsBitVect(mol_a, 2, nBits=2048)
    fp_b = AllChem.GetMorganFingerprintAsBitVect(mol_b, 2, nBits=2048)
    return float(DataStructs.TanimotoSimilarity(fp_a, fp_b))


def calculate_sa_score(smiles: str) -> float | None:
    rdkit_smiles = to_rdkit_smiles(smiles)
    if rdkit_smiles is None:
        return None

    mol = Chem.MolFromSmiles(rdkit_smiles)
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


def _candidate_sort_key(candidate: ConditionalGenerationCandidate) -> tuple[float, float, float, str]:
    similarity = -1e18 if candidate.similarity_score is None else -candidate.similarity_score
    sa_score = 1e18 if candidate.sa_score is None else candidate.sa_score
    return (similarity, sa_score, candidate.generated_smiles)


def run_conditional_generation(
    *,
    input_smiles: str,
    delta_tg: float,
    candidate_count: int,
    top_k: int,
    temperature: float,
    runtime: ConditionalGenerationRuntime,
    max_attempts: int | None = None,
    max_length: int = DEFAULT_MAX_SMILES_LEN,
) -> ConditionalGenerationResult:
    input_smiles_model = to_model_smiles(input_smiles)
    input_smiles_rdkit = to_rdkit_smiles(input_smiles_model)

    if input_smiles_model is None or input_smiles_rdkit is None:
        raise ValueError("invalid smiles")
    if count_attachment_points(input_smiles_rdkit) < 2:
        raise ValueError("input polymer must contain at least two attachment points")

    requested_count = int(candidate_count)
    condition_delta_tg = float(delta_tg)
    max_attempt_count = max_attempts if max_attempts is not None else max(requested_count * 5, 20)
    attempts = 0
    seen: set[str] = set()
    candidates: list[ConditionalGenerationCandidate] = []
    filter_counter: Counter[str] = Counter()

    while len(candidates) < requested_count and attempts < max_attempt_count:
        attempts += 1
        # Invalid generated strings are ordinary filtering outcomes below.
        # Exceptions from model execution are runtime failures (including OOM)
        # and must reach the Registry/job manager instead of being converted
        # into a misleading successful response with zero candidates.
        generated = runtime.generate_once(
            input_smiles=input_smiles_model,
            delta_tg=condition_delta_tg,
            top_k=top_k,
            temperature=temperature,
            max_length=max_length,
        )

        raw_smiles = generated.raw_smiles.strip()
        rdkit_smiles = generated.rdkit_smiles or to_rdkit_smiles(raw_smiles)

        if not raw_smiles:
            filter_counter["empty_raw_smiles"] += 1
            continue
        if rdkit_smiles is None or Chem.MolFromSmiles(rdkit_smiles) is None:
            filter_counter["rdkit_parse_failed"] += 1
            continue
        if count_attachment_points(rdkit_smiles) < 2:
            filter_counter["star_count_lt_2"] += 1
            continue
        if rdkit_smiles == input_smiles_rdkit:
            filter_counter["same_as_input"] += 1
            continue
        if rdkit_smiles in seen:
            filter_counter["duplicate"] += 1
            continue
        if not is_valid_polymer(rdkit_smiles):
            filter_counter["invalid_polymer"] += 1
            continue

        seen.add(rdkit_smiles)
        # The evaluator and scaler are part of the same runtime contract.
        # Their failures are not candidate-level business errors.
        predicted_tg = float(runtime.predict_tg(rdkit_smiles))

        candidates.append(
            ConditionalGenerationCandidate(
                rank=0,
                generated_smiles=rdkit_smiles,
                predicted_tg=predicted_tg,
                tg_error=None,
                similarity_score=calculate_similarity(input_smiles_rdkit, rdkit_smiles),
                sa_score=calculate_sa_score(rdkit_smiles),
            )
        )

    candidates.sort(key=_candidate_sort_key)
    for index, candidate in enumerate(candidates, start=1):
        candidate.rank = index

    return ConditionalGenerationResult(
        input_smiles_model=input_smiles_model,
        input_smiles_rdkit=input_smiles_rdkit,
        delta_tg=condition_delta_tg,
        requested_count=requested_count,
        attempts=attempts,
        filter_counter=dict(filter_counter),
        candidates=candidates,
    )
