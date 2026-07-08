from __future__ import annotations

import importlib
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from rdkit import Chem
from rdkit.Chem import RDConfig

from .config import WorkerSettings


REQUIRED_MODEL_FILES: tuple[str, ...] = (
    "config.json",
    "pytorch_model.bin",
    "tokenizer.json",
    "spiece.model",
)


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    model_files_ready: bool
    runtime_ready: bool
    runtime_error: str | None = None


@dataclass(slots=True)
class PolytaoCandidate:
    rank: int
    generated_smiles: str
    raw_smiles: str
    valid_smiles: bool = True
    sa_score: float | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PolytaoGenerationResult:
    result: dict[str, Any]
    query_time_ms: float
    returned_count: int


class PolytaoRuntime:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._lock = Lock()
        self._loaded = False
        self._tokenizer: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._device = "cpu"

    def probe(self) -> RuntimeProbe:
        missing = missing_model_files(self._settings.model_dir)
        if missing:
            return RuntimeProbe(
                model_files_ready=False,
                runtime_ready=False,
                runtime_error="missing PolyTAO model files: " + ", ".join(missing),
            )
        try:
            importlib.import_module("torch")
            importlib.import_module("transformers")
            importlib.import_module("rdkit")
        except Exception as exc:
            return RuntimeProbe(
                model_files_ready=True,
                runtime_ready=False,
                runtime_error=f"runtime dependency import failed: {exc}",
            )

        try:
            self._resolve_device()
        except Exception as exc:
            return RuntimeProbe(
                model_files_ready=True,
                runtime_ready=False,
                runtime_error=str(exc),
            )
        return RuntimeProbe(model_files_ready=True, runtime_ready=True)

    def generate(
        self,
        *,
        prompt: str,
        candidate_count: int,
        temperature: float,
        top_k: int,
        top_p: float,
        max_length: int,
    ) -> PolytaoGenerationResult:
        tokenizer, model, torch, device = self._load()
        started_at = time.perf_counter()
        raw_candidates: list[str] = []
        requested = max(1, int(candidate_count))
        max_raw_candidates = min(max(requested * 10, requested), 100)

        while len(raw_candidates) < max_raw_candidates:
            batch_count = min(max(requested * 3, requested), max_raw_candidates - len(raw_candidates))
            encoded = tokenizer(prompt, return_tensors="pt")
            encoded = {key: value.to(device) for key, value in encoded.items()}
            with torch.inference_mode():
                outputs = model.generate(
                    **encoded,
                    do_sample=True,
                    temperature=float(temperature),
                    top_k=int(top_k),
                    top_p=float(top_p),
                    max_length=int(max_length),
                    num_return_sequences=batch_count,
                    pad_token_id=tokenizer.pad_token_id,
                    eos_token_id=tokenizer.eos_token_id,
                )
            raw_candidates.extend(
                tokenizer.decode(output, skip_special_tokens=True)
                for output in outputs
            )
            accepted, _filters = normalize_polytao_candidates(raw_candidates, requested)
            if len(accepted) >= requested:
                break

        accepted, filters = normalize_polytao_candidates(raw_candidates, requested)
        query_time_ms = (time.perf_counter() - started_at) * 1000
        result = {
            "prompt": prompt,
            "query_time_ms": query_time_ms,
            "requested_count": requested,
            "returned_count": len(accepted),
            "attempts": 1,
            "filter_counter": filters,
            "results": [
                {
                    "rank": candidate.rank,
                    "generated_smiles": candidate.generated_smiles,
                    "raw_smiles": candidate.raw_smiles,
                    "valid_smiles": candidate.valid_smiles,
                    "sa_score": candidate.sa_score,
                    "warnings": candidate.warnings,
                }
                for candidate in accepted
            ],
        }
        return PolytaoGenerationResult(
            result=result,
            query_time_ms=query_time_ms,
            returned_count=len(accepted),
        )

    def _load(self) -> tuple[Any, Any, Any, str]:
        if self._loaded:
            return self._tokenizer, self._model, self._torch, self._device
        with self._lock:
            if self._loaded:
                return self._tokenizer, self._model, self._torch, self._device
            missing = missing_model_files(self._settings.model_dir)
            if missing:
                raise RuntimeError("missing PolyTAO model files: " + ", ".join(missing))

            torch = importlib.import_module("torch")
            transformers = importlib.import_module("transformers")
            device = self._resolve_device(torch)
            tokenizer = transformers.AutoTokenizer.from_pretrained(
                str(self._settings.model_dir),
                local_files_only=True,
            )
            model = transformers.AutoModelForSeq2SeqLM.from_pretrained(
                str(self._settings.model_dir),
                local_files_only=True,
            )
            model.to(device)
            model.eval()

            self._tokenizer = tokenizer
            self._model = model
            self._torch = torch
            self._device = device
            self._loaded = True
            return tokenizer, model, torch, device

    def _resolve_device(self, torch: Any | None = None) -> str:
        selected = self._settings.device
        if torch is None:
            torch = importlib.import_module("torch")
        if selected in {"", "auto"}:
            return "cuda" if torch.cuda.is_available() else "cpu"
        if selected.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(f"POLYTAO_DEVICE={selected} requested but CUDA is not available")
        return selected


def missing_model_files(model_dir: Path) -> list[str]:
    return [filename for filename in REQUIRED_MODEL_FILES if not (model_dir / filename).is_file()]


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
) -> tuple[list[PolytaoCandidate], dict[str, int]]:
    seen: set[str] = set()
    accepted: list[PolytaoCandidate] = []
    filters: Counter[str] = Counter()

    for raw in raw_candidates:
        raw_smiles = str(raw).strip()
        if not raw_smiles:
            filters["empty_raw_smiles"] += 1
            continue
        normalized = normalize_polytao_smiles(raw_smiles)
        if normalized is None:
            filters["rdkit_parse_failed"] += 1
            continue
        if count_attachment_points(normalized) < 2:
            filters["star_count_lt_2"] += 1
            continue
        if normalized in seen:
            filters["duplicate"] += 1
            continue
        seen.add(normalized)
        accepted.append(
            PolytaoCandidate(
                rank=len(accepted) + 1,
                generated_smiles=normalized,
                raw_smiles=raw_smiles,
                sa_score=calculate_sa_score(normalized),
            )
        )
        if len(accepted) >= requested_count:
            break
    return accepted, dict(filters)
