from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from typing import Any

from rdkit import Chem

from app.models import (
    MonomerRetrosynthesisCandidate,
    MonomerRetrosynthesisResponse,
    RetrosynthesisReactant,
    RetrosynthesisTargetRole,
)
from app.utils.exceptions import InvalidSmilesError, ModelArtifactError


_RUNTIME_LOCK = Lock()
_RUNTIME_CACHE: dict[tuple[str, str], "_ReactionT5Runtime"] = {}

_AMINE_SMARTS = Chem.MolFromSmarts("[NX3;H2,H1;!$(NC=O)]")
_ANHYDRIDE_SMARTS = Chem.MolFromSmarts("[CX3](=O)O[CX3](=O)")
_NITRO_SMARTS = Chem.MolFromSmarts("[NX3+](=O)[O-]")
_CARBOXYLIC_ACID_SMARTS = Chem.MolFromSmarts("[CX3](=O)[OX2H1]")


@dataclass(frozen=True)
class _ReactionT5Runtime:
    tokenizer: Any
    model: Any
    torch: Any
    device: str


def predict_monomer_precursors(
    smiles: str,
    *,
    target_role: RetrosynthesisTargetRole,
    num_beams: int,
    num_return_sequences: int,
    max_new_tokens: int,
    model_id: str,
    device: str,
) -> MonomerRetrosynthesisResponse:
    started_at = perf_counter()
    canonical_smiles, target_mol = _canonicalize_smiles(smiles)
    resolved_device = _resolve_device(device)
    runtime = _get_runtime(model_id, resolved_device)

    inputs = runtime.tokenizer(canonical_smiles, return_tensors="pt")
    inputs = {key: value.to(runtime.device) for key, value in inputs.items()}

    try:
        with runtime.torch.inference_mode():
            output = runtime.model.generate(
                **inputs,
                num_beams=num_beams,
                num_return_sequences=num_return_sequences,
                max_new_tokens=max_new_tokens,
                return_dict_in_generate=True,
            )
    except Exception as exc:  # pragma: no cover - depends on accelerator/runtime state
        raise ModelArtifactError(f"retrosynthesis inference failed: {exc}") from exc

    candidates = _decode_candidates(
        output=output,
        runtime=runtime,
        target_mol=target_mol,
        inferred_role=_infer_target_role(target_mol),
    )

    return MonomerRetrosynthesisResponse(
        input_smiles=smiles,
        canonical_smiles=canonical_smiles,
        target_role=target_role,
        inferred_target_role=_resolve_target_role(target_role, target_mol),
        model_id=model_id,
        device=resolved_device,
        query_time_ms=(perf_counter() - started_at) * 1000,
        total=len(candidates),
        candidates=candidates,
    )


def _get_runtime(model_id: str, device: str) -> _ReactionT5Runtime:
    cache_key = (model_id, device)
    with _RUNTIME_LOCK:
        cached = _RUNTIME_CACHE.get(cache_key)
        if cached is not None:
            return cached

        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - dependency guard for partial installs
            raise ModelArtifactError("retrosynthesis dependencies are not installed") from exc

        load_kwargs = {"local_files_only": True}
        try:
            tokenizer = AutoTokenizer.from_pretrained(model_id, **load_kwargs)
            model = AutoModelForSeq2SeqLM.from_pretrained(model_id, **load_kwargs)
            model.to(device)
            model.eval()
        except Exception as exc:  # pragma: no cover - depends on local model cache/network
            raise ModelArtifactError(
                "retrosynthesis model files are not available locally; "
                "please prepare the model files before running this feature"
            ) from exc

        runtime = _ReactionT5Runtime(tokenizer=tokenizer, model=model, torch=torch, device=device)
        _RUNTIME_CACHE[cache_key] = runtime
        return runtime


def _resolve_device(device: str) -> str:
    normalized = (device or "auto").strip().lower()
    if normalized == "auto":
        try:
            import torch
        except ImportError as exc:  # pragma: no cover - dependency guard for partial installs
            raise ModelArtifactError("retrosynthesis dependencies are not installed") from exc
        return "cpu" if _cuda_support_error(torch) else "cuda"
    if normalized not in {"cpu", "cuda", "mps"}:
        raise ModelArtifactError("RETRO_DEVICE must be one of auto, cpu, cuda, or mps")
    if normalized == "cuda":
        try:
            import torch
        except ImportError as exc:  # pragma: no cover - dependency guard for partial installs
            raise ModelArtifactError("retrosynthesis dependencies are not installed") from exc
        support_error = _cuda_support_error(torch)
        if support_error:
            raise ModelArtifactError(support_error)
    return normalized


def _cuda_support_error(torch_module: Any) -> str | None:
    if not torch_module.cuda.is_available():
        return "CUDA is not available"
    try:
        torch_module.empty(1, device="cuda")
        sample = torch_module.empty((1, 1, 3, 3), device="cuda")
        kernel = torch_module.ones((1, 1, 1, 1), device="cuda")
        torch_module.nn.functional.conv2d(sample, kernel)
        torch_module.cuda.synchronize()
    except Exception as exc:  # pragma: no cover - depends on CUDA runtime
        return f"CUDA capability check failed: {exc}"
    return None


def _canonicalize_smiles(smiles: str) -> tuple[str, Chem.Mol]:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise InvalidSmilesError(f"invalid smiles: {smiles}")
    return Chem.MolToSmiles(mol, canonical=True), mol


def _decode_candidates(
    *,
    output: Any,
    runtime: _ReactionT5Runtime,
    target_mol: Chem.Mol,
    inferred_role: str,
) -> list[MonomerRetrosynthesisCandidate]:
    seen: set[str] = set()
    candidates: list[MonomerRetrosynthesisCandidate] = []

    for index, sequence in enumerate(output.sequences):
        raw_output = runtime.tokenizer.decode(sequence, skip_special_tokens=True)
        reactants_smiles = _clean_reactants(raw_output)
        if not reactants_smiles:
            continue

        reactants = [_build_reactant(part) for part in reactants_smiles.split(".") if part]
        canonical_parts = [
            reactant.canonical_smiles for reactant in reactants if reactant.canonical_smiles
        ]
        valid_smiles = bool(reactants) and len(canonical_parts) == len(reactants)
        canonical_reactants = ".".join(canonical_parts) if valid_smiles else None
        dedupe_key = canonical_reactants or reactants_smiles
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        candidates.append(
            MonomerRetrosynthesisCandidate(
                rank=len(candidates) + 1,
                raw_output=raw_output,
                reactants_smiles=reactants_smiles,
                canonical_reactants_smiles=canonical_reactants,
                reactants=reactants,
                valid_smiles=valid_smiles,
                all_reactants_smaller_than_target=_all_reactants_smaller(target_mol, reactants)
                if valid_smiles
                else None,
                reaction_hint=_reaction_hint(inferred_role, reactants),
            )
        )

    return candidates


def _clean_reactants(raw_output: str) -> str:
    return raw_output.replace(" ", "").strip().rstrip(".")


def _build_reactant(smiles: str) -> RetrosynthesisReactant:
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return RetrosynthesisReactant(input_smiles=smiles, valid_smiles=False)
    return RetrosynthesisReactant(
        input_smiles=smiles,
        canonical_smiles=Chem.MolToSmiles(mol, canonical=True),
        valid_smiles=True,
        heavy_atom_count=mol.GetNumHeavyAtoms(),
    )


def _all_reactants_smaller(target_mol: Chem.Mol, reactants: list[RetrosynthesisReactant]) -> bool:
    target_heavy_atoms = target_mol.GetNumHeavyAtoms()
    return all(
        reactant.heavy_atom_count is not None and reactant.heavy_atom_count < target_heavy_atoms
        for reactant in reactants
    )


def _resolve_target_role(
    target_role: RetrosynthesisTargetRole,
    target_mol: Chem.Mol,
) -> str:
    if target_role != "auto":
        return target_role
    return _infer_target_role(target_mol)


def _infer_target_role(target_mol: Chem.Mol) -> str:
    amine_count = len(target_mol.GetSubstructMatches(_AMINE_SMARTS)) if _AMINE_SMARTS else 0
    anhydride_count = (
        len(target_mol.GetSubstructMatches(_ANHYDRIDE_SMARTS)) if _ANHYDRIDE_SMARTS else 0
    )
    if anhydride_count >= 2:
        return "dianhydride"
    if amine_count >= 2:
        return "diamine"
    return "other"


def _reaction_hint(inferred_role: str, reactants: list[RetrosynthesisReactant]) -> str:
    valid_mols = [
        Chem.MolFromSmiles(reactant.canonical_smiles)
        for reactant in reactants
        if reactant.canonical_smiles
    ]
    nitro_count = sum(
        len(mol.GetSubstructMatches(_NITRO_SMARTS)) for mol in valid_mols if mol is not None and _NITRO_SMARTS
    )
    acid_count = sum(
        len(mol.GetSubstructMatches(_CARBOXYLIC_ACID_SMARTS))
        for mol in valid_mols
        if mol is not None and _CARBOXYLIC_ACID_SMARTS
    )

    if inferred_role == "diamine" and nitro_count >= 2:
        return "二硝基前体还原候选"
    if inferred_role == "dianhydride" and acid_count >= 2:
        return "羧酸/四酸脱水成酐候选"
    if inferred_role == "dianhydride":
        return "二酐上游反应物候选"
    if inferred_role == "diamine":
        return "二胺上游反应物候选"
    return "逆合成候选"
