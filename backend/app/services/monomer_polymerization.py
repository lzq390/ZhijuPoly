from __future__ import annotations

import importlib
import time
from dataclasses import dataclass
from typing import Any

from rdkit import Chem

from app.models import (
    MonomerPolymerizationInput,
    MonomerPolymerizationRequest,
    MonomerPolymerizationResponse,
    MonomerPolymerizationStatusResponse,
    MonomerPolymerizationTargetRequirement,
    PolymerizationTargetClass,
)
from app.services.smiles_utils import standardize_smiles
from app.services.structure_2d import generate_2d_svg
from app.utils.exceptions import InvalidSmilesError, ModelArtifactError


DEFAULT_TARGET_CLASS: PolymerizationTargetClass = "polyimide"
MAX_RESULTS_LIMIT = 20
TARGET_CLASS_ORDER: tuple[PolymerizationTargetClass, ...] = (
    "polyolefin",
    "polyester",
    "polyether",
    "polyamide",
    "polyimide",
    "polyurethane",
    "polyoxazolidone",
    "all",
)
TARGET_CLASS_MIN_MONOMERS: dict[PolymerizationTargetClass, int] = {
    "polyolefin": 1,
    "polyester": 2,
    "polyether": 1,
    "polyamide": 2,
    "polyimide": 2,
    "polyurethane": 2,
    "polyoxazolidone": 1,
    "all": 1,
}
TARGET_CLASS_REQUIREMENT_NOTES: dict[PolymerizationTargetClass, str] = {
    "polyolefin": "Allows a single submitted monomer for chain-growth rules.",
    "polyester": "Requires two complementary monomers for the lightweight v1 workflow.",
    "polyether": "Allows a single submitted monomer when SMiPoly has a matching rule.",
    "polyamide": "Requires two complementary monomers for the lightweight v1 workflow.",
    "polyimide": "Requires a diamine and a dianhydride monomer.",
    "polyurethane": "Requires two complementary monomers for the lightweight v1 workflow.",
    "polyoxazolidone": "Allows a single submitted monomer when SMiPoly has a matching rule.",
    "all": "Allows a single submitted monomer and searches across available rule classes.",
}


@dataclass(frozen=True)
class SmipolyRuntime:
    pd: Any
    monc: Any
    polg: Any
    target_classes: tuple[str, ...]


def get_monomer_polymerization_status(enabled: bool) -> MonomerPolymerizationStatusResponse:
    if not enabled:
        return MonomerPolymerizationStatusResponse(
            enabled=False,
            available=False,
            available_target_classes=list(TARGET_CLASS_ORDER),
            target_requirements=_target_requirements(),
            message="monomer polymerization service is disabled",
        )

    try:
        runtime = _load_smipoly_runtime()
    except ModelArtifactError as exc:
        return MonomerPolymerizationStatusResponse(
            enabled=True,
            available=False,
            available_target_classes=list(TARGET_CLASS_ORDER),
            target_requirements=_target_requirements(),
            message=str(exc),
        )

    target_classes = _ordered_target_classes(runtime.target_classes)
    return MonomerPolymerizationStatusResponse(
        enabled=True,
        available=True,
        available_target_classes=target_classes,
        target_requirements=_target_requirements(),
        message="SMiPoly rule polymerization service is available",
    )


def run_monomer_polymerization(
    request: MonomerPolymerizationRequest,
) -> MonomerPolymerizationResponse:
    started_at = time.perf_counter()
    _validate_target_monomer_count(request)
    input_monomers = _canonicalize_inputs(request)
    runtime = _load_smipoly_runtime()

    rows = [
        {"label": index + 1, "SMILES": monomer.canonical_smiles}
        for index, monomer in enumerate(input_monomers)
    ]
    source_df = runtime.pd.DataFrame(rows)

    try:
        classified_df = runtime.monc.moncls(source_df, smiColn="SMILES", dsp_rsl=False)
        generated_df = runtime.polg.biplym(
            classified_df,
            targ=["all"] if request.target_class == "all" else [request.target_class],
            dsp_rsl=False,
        )
    except Exception as exc:  # pragma: no cover - exercised through route-level 503 tests
        raise ModelArtifactError("SMiPoly polymerization failed") from exc

    if generated_df is None:
        raise ModelArtifactError("SMiPoly rejected the requested polymer class")

    input_key_by_smiles = {
        monomer.canonical_smiles: _smiles_key(monomer.canonical_smiles)
        for monomer in input_monomers
    }
    allowed_input_keys = set(input_key_by_smiles.values())
    requested_two_monomers = len(input_monomers) == 2
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str, str, int | None]] = set()
    filtered_auxiliary_rows = 0

    for _, row in generated_df.iterrows():
        monomer_a = _clean_text(row.get("mon1"))
        monomer_b = _clean_optional_text(row.get("mon2"))
        polymer_smiles = _clean_text(row.get("polym"))
        polymer_class = _clean_text(row.get("polymer_class"))
        reaction_id = _optional_int(row.get("Ps_rxnL"))

        if not monomer_a or not polymer_smiles:
            continue

        monomer_a_key = _smiles_key(monomer_a)
        monomer_b_key = _smiles_key(monomer_b) if monomer_b else None
        uses_requested_input = monomer_a_key in allowed_input_keys and (
            monomer_b_key in allowed_input_keys if requested_two_monomers else monomer_b_key in {None, *allowed_input_keys}
        )
        if requested_two_monomers and {monomer_a_key, monomer_b_key} != allowed_input_keys:
            uses_requested_input = False

        if not uses_requested_input:
            filtered_auxiliary_rows += 1
            continue

        dedupe_key = (monomer_a, monomer_b, polymer_smiles, polymer_class, reaction_id)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        candidates.append(
            {
                "rank": 1,
                "monomer_a_smiles": monomer_a,
                "monomer_b_smiles": monomer_b,
                "polymer_smiles": polymer_smiles,
                "polymer_class": polymer_class,
                "reaction_id": reaction_id,
                "reaction_name": None,
                "reactset": _reactset_to_list(row.get("reactset")),
                "structure_svg": generate_2d_svg(polymer_smiles),
            }
        )

    candidates.sort(
        key=lambda item: (
            item["polymer_class"],
            item["reaction_id"] if item["reaction_id"] is not None else 10**9,
            item["polymer_smiles"],
            item["monomer_a_smiles"],
            item["monomer_b_smiles"] or "",
        )
    )
    total = len(candidates)
    returned_candidates = candidates[: request.max_results]
    for index, candidate in enumerate(returned_candidates, start=1):
        candidate["rank"] = index

    warnings: list[str] = []
    if len(input_monomers) == 1:
        warnings.append(
            "Single-monomer requests only return polymerizations that do not require another user-provided monomer."
        )
    if filtered_auxiliary_rows:
        warnings.append(
            "Filtered SMiPoly rows that involved automatically added auxiliary molecules outside the submitted monomers."
        )
    if total == 0:
        warnings.append("SMiPoly generated no polymer candidates for the supplied monomer(s) and target class.")

    return MonomerPolymerizationResponse(
        input_monomers=input_monomers,
        target_class=request.target_class,
        query_time_ms=(time.perf_counter() - started_at) * 1000,
        total=total,
        results=returned_candidates,
        warnings=warnings,
    )


def _load_smipoly_runtime() -> SmipolyRuntime:
    try:
        pd = importlib.import_module("pandas")
        monc = importlib.import_module("smipoly.smip.monc")
        polg = importlib.import_module("smipoly.smip.polg")
    except Exception as exc:
        raise ModelArtifactError("SMiPoly is not installed or cannot load its rule files") from exc

    target_classes = tuple(getattr(polg, "Ps_classL", {}).keys())
    if not target_classes:
        target_classes = tuple(item for item in TARGET_CLASS_ORDER if item != "all")
    return SmipolyRuntime(pd=pd, monc=monc, polg=polg, target_classes=target_classes)


def _target_requirements() -> dict[PolymerizationTargetClass, MonomerPolymerizationTargetRequirement]:
    return {
        target_class: MonomerPolymerizationTargetRequirement(
            min_monomers=TARGET_CLASS_MIN_MONOMERS[target_class],
            max_monomers=2,
            monomer_b_required=TARGET_CLASS_MIN_MONOMERS[target_class] > 1,
            note=TARGET_CLASS_REQUIREMENT_NOTES[target_class],
        )
        for target_class in TARGET_CLASS_ORDER
    }


def _validate_target_monomer_count(request: MonomerPolymerizationRequest) -> None:
    min_monomers = TARGET_CLASS_MIN_MONOMERS.get(request.target_class, 1)
    has_monomer_b = request.monomer_b_smiles is not None and bool(request.monomer_b_smiles.strip())
    if min_monomers > 1 and not has_monomer_b:
        raise InvalidSmilesError(f"target_class {request.target_class} requires monomer_b_smiles")


def _canonicalize_inputs(request: MonomerPolymerizationRequest) -> list[MonomerPolymerizationInput]:
    inputs = [
        MonomerPolymerizationInput(
            role="monomer_a",
            input_smiles=request.monomer_a_smiles,
            canonical_smiles=_canonicalize_monomer_smiles("monomer_a_smiles", request.monomer_a_smiles),
        )
    ]
    if request.monomer_b_smiles is not None:
        inputs.append(
            MonomerPolymerizationInput(
                role="monomer_b",
                input_smiles=request.monomer_b_smiles,
                canonical_smiles=_canonicalize_monomer_smiles("monomer_b_smiles", request.monomer_b_smiles),
            )
        )
    return inputs


def _canonicalize_monomer_smiles(field_name: str, smiles: str) -> str:
    value = smiles.strip()
    if not value:
        raise InvalidSmilesError(f"{field_name} must not be empty")

    mol = Chem.MolFromSmiles(value)
    if mol is None:
        raise InvalidSmilesError(f"invalid smiles in {field_name}: {smiles}")
    if any(atom.GetAtomicNum() == 0 for atom in mol.GetAtoms()):
        raise InvalidSmilesError(f"{field_name} must be ordinary monomer SMILES without attachment points")

    try:
        return standardize_smiles(value)
    except ValueError as exc:
        raise InvalidSmilesError(f"invalid smiles in {field_name}: {smiles}") from exc


def _ordered_target_classes(target_classes: tuple[str, ...]) -> list[PolymerizationTargetClass]:
    available = set(target_classes)
    ordered = [target for target in TARGET_CLASS_ORDER if target == "all" or target in available]
    if "all" not in ordered:
        ordered.append("all")
    return ordered


def _smiles_key(smiles: str | None) -> str | None:
    if smiles is None:
        return None
    value = smiles.strip()
    if not value:
        return None
    try:
        return standardize_smiles(value)
    except ValueError:
        return value


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if bool(importlib.import_module("pandas").isna(value)):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _clean_optional_text(value: Any) -> str | None:
    text = _clean_text(value)
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if bool(importlib.import_module("pandas").isna(value)):
            return None
    except Exception:
        pass
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reactset_to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []
