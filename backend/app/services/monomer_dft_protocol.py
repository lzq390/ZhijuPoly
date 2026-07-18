from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any

from .monomer_dft_models import (
    MonomerDftOptimizationRequest,
    MonomerDftRunRequest,
    MonomerDftSinglePointRequest,
)


STANDARD_ELEMENTS = frozenset({1, 5, 6, 7, 8, 9, 14, 15, 16, 17, 33, 34, 35, 53})
PD_ELEMENTS = frozenset({1, 5, 6, 7, 8, 9, 14, 15, 16, 17, 34, 35, 46, 53})
RXN_ELEMENTS = frozenset({1, 6, 7, 8})
MODEL_ELEMENTS = {
    "aimnet2": STANDARD_ELEMENTS,
    "aimnet2-2025": STANDARD_ELEMENTS,
    "aimnet2-b973c": STANDARD_ELEMENTS,
    "aimnet2-nse": STANDARD_ELEMENTS,
    "aimnet2-pd": PD_ELEMENTS,
    "aimnet2-rxn": RXN_ELEMENTS,
}
MAX_HEAVY_ATOMS = 100
MAX_TOTAL_ATOMS = 300
MAX_HESSIAN_ATOMS = 100


class MonomerDftRequestError(ValueError):
    """A public, path-free validation error for a calculation request."""

    def __init__(self, message: str, *, code: str = "invalid_scientific_request") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PreparedMonomerDftRequest:
    public_request: dict[str, Any]
    worker_request: dict[str, Any]
    request_sha256: str
    canonical_smiles: str
    input_type: str
    formal_charge: int
    effective_charge: int
    electron_count: int
    atomic_numbers: tuple[int, ...]
    isotope_mass_numbers: tuple[int, ...]
    atomic_masses_u: tuple[float, ...]
    total_atoms: int
    heavy_atoms: int
    warnings: tuple[str, ...]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def calculation_request_sha256(worker_request: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(worker_request).encode("utf-8")).hexdigest()


def _load_rdkit():
    try:
        from rdkit import Chem
    except ImportError as exc:  # pragma: no cover - deployment dependency failure
        raise RuntimeError("molecular request validation is unavailable") from exc
    return Chem


def _canonicalize_and_validate_molecule(
    smiles: str,
    psmiles_mode: str | None,
) -> tuple[
    str,
    str,
    int,
    int,
    int,
    tuple[int, ...],
    tuple[int, ...],
    tuple[float, ...],
]:
    if "." in smiles:
        raise MonomerDftRequestError("input.smiles must not contain multiple molecular fragments")

    Chem = _load_rdkit()
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise MonomerDftRequestError("input.smiles is not a valid SMILES string")
    if len(Chem.GetMolFrags(molecule)) != 1:
        raise MonomerDftRequestError("input.smiles must contain exactly one molecular fragment")
    dummy_indices = [atom.GetIdx() for atom in molecule.GetAtoms() if atom.GetAtomicNum() == 0]
    if not dummy_indices and psmiles_mode is not None:
        raise MonomerDftRequestError("input.psmiles_mode must be null for ordinary SMILES")
    if dummy_indices and psmiles_mode is None:
        raise MonomerDftRequestError("input.psmiles_mode is required when input.smiles contains attachment points")
    if psmiles_mode == "close" and len(dummy_indices) != 2:
        raise MonomerDftRequestError("PSMILES close mode requires exactly two attachment points")
    if psmiles_mode == "cap" and not dummy_indices:
        raise MonomerDftRequestError("PSMILES cap mode requires at least one attachment point")

    if dummy_indices:
        boundary_indices: list[int] = []
        boundary_bond_types: list[Any] = []
        for dummy_index in dummy_indices:
            dummy = molecule.GetAtomWithIdx(dummy_index)
            neighbors = list(dummy.GetNeighbors())
            if len(neighbors) != 1:
                raise MonomerDftRequestError("each PSMILES attachment point must have exactly one neighbor")
            boundary = neighbors[0]
            boundary_indices.append(boundary.GetIdx())
            boundary_bond = molecule.GetBondBetweenAtoms(dummy_index, boundary.GetIdx())
            if boundary_bond is None:
                raise MonomerDftRequestError("PSMILES attachment point bond is missing")
            boundary_bond_types.append(boundary_bond.GetBondType())

        editable = Chem.RWMol(molecule)
        if psmiles_mode == "close":
            left, right = boundary_indices
            if left == right:
                raise MonomerDftRequestError("PSMILES attachment points must connect to different atoms")
            if boundary_bond_types[0] != boundary_bond_types[1]:
                raise MonomerDftRequestError(
                    "PSMILES close mode requires matching attachment bond types"
                )
            if editable.GetBondBetweenAtoms(left, right) is None:
                bond_type = boundary_bond_types[0]
                if bond_type == Chem.BondType.UNSPECIFIED:
                    bond_type = Chem.BondType.SINGLE
                editable.AddBond(left, right, bond_type)
        for dummy_index in sorted(dummy_indices, reverse=True):
            editable.RemoveAtom(dummy_index)
        molecule = editable.GetMol()
        try:
            Chem.SanitizeMol(molecule)
        except Exception as exc:
            raise MonomerDftRequestError("PSMILES could not be converted into a valid finite molecule") from exc
        if len(Chem.GetMolFrags(molecule)) != 1:
            raise MonomerDftRequestError(
                "processed PSMILES must produce exactly one connected molecule"
            )

    canonical_smiles = Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)
    molecule_with_hydrogens = Chem.AddHs(molecule)
    atomic_numbers = tuple(
        int(atom.GetAtomicNum()) for atom in molecule_with_hydrogens.GetAtoms()
    )
    isotope_mass_numbers = tuple(
        int(atom.GetIsotope()) for atom in molecule_with_hydrogens.GetAtoms()
    )
    periodic_table = Chem.GetPeriodicTable()
    atomic_masses: list[float] = []
    for atomic_number, isotope in zip(
        atomic_numbers,
        isotope_mass_numbers,
        strict=True,
    ):
        if isotope == 0:
            mass = float(periodic_table.GetAtomicWeight(atomic_number))
        else:
            try:
                mass = float(
                    periodic_table.GetMassForIsotope(atomic_number, isotope)
                )
            except Exception:
                mass = 0.0
        if not math.isfinite(mass) or mass <= 0.0:
            raise MonomerDftRequestError(
                f"isotope {isotope} is not supported for atomic number {atomic_number}",
                code="unsupported_isotope",
            )
        atomic_masses.append(mass)
    total_atoms = int(molecule_with_hydrogens.GetNumAtoms())
    heavy_atoms = int(molecule.GetNumHeavyAtoms())
    nuclear_charge = sum(int(atom.GetAtomicNum()) for atom in molecule_with_hydrogens.GetAtoms())
    input_type = "smiles" if psmiles_mode is None else f"psmiles_{psmiles_mode}"
    return (
        canonical_smiles,
        input_type,
        total_atoms,
        heavy_atoms,
        nuclear_charge,
        atomic_numbers,
        isotope_mass_numbers,
        tuple(atomic_masses),
    )


def _validate_model_domain(
    request: MonomerDftSinglePointRequest | MonomerDftOptimizationRequest,
    *,
    electron_count: int,
    effective_charge: int,
    atomic_numbers: set[int],
) -> None:
    allowed = MODEL_ELEMENTS[request.model]
    unsupported = sorted(atomic_numbers - allowed)
    if unsupported:
        joined = ", ".join(str(item) for item in unsupported)
        raise MonomerDftRequestError(f"model {request.model} does not support atomic number(s): {joined}")
    if request.model == "aimnet2-rxn" and effective_charge != 0:
        raise MonomerDftRequestError("model aimnet2-rxn supports only net-neutral molecules")
    if request.model != "aimnet2-nse" and request.input.multiplicity != 1:
        raise MonomerDftRequestError("multiplicity greater than 1 requires model aimnet2-nse")

    unpaired_electrons = request.input.multiplicity - 1
    if electron_count < 1:
        raise MonomerDftRequestError("input.net_charge leaves the molecule with no electrons")
    if electron_count < unpaired_electrons or (electron_count - unpaired_electrons) % 2 != 0:
        raise MonomerDftRequestError("input charge and multiplicity are not compatible with the molecular electron count")


def _requested_properties(
    request: MonomerDftSinglePointRequest | MonomerDftOptimizationRequest,
) -> list[str]:
    if isinstance(request, MonomerDftSinglePointRequest):
        return list(request.single_point.properties)
    return list(request.optimization.post_optimization_properties)


def prepare_monomer_dft_request(
    request: MonomerDftRunRequest,
) -> PreparedMonomerDftRequest:
    (
        canonical_smiles,
        input_type,
        total_atoms,
        heavy_atoms,
        nuclear_charge,
        atomic_numbers,
        isotope_mass_numbers,
        atomic_masses_u,
    ) = _canonicalize_and_validate_molecule(
        request.input.smiles,
        request.input.psmiles_mode,
    )
    effective_charge = request.input.net_charge
    formal_charge = _formal_charge(canonical_smiles)
    if effective_charge is None:
        effective_charge = formal_charge
    if not -5 <= effective_charge <= 5:
        raise MonomerDftRequestError(
            "effective net charge must be between -5 and 5",
            code="charge_out_of_range",
        )
    warnings: list[str] = []
    if request.input.net_charge is not None:
        if effective_charge != formal_charge:
            warnings.append(
                "Explicit net_charge overrides SMILES charge inference and differs "
                "from the encoded formal charge."
            )
        else:
            warnings.append(
                "Explicit net_charge overrides SMILES charge inference and matches "
                "the encoded formal charge."
            )
    if heavy_atoms > MAX_HEAVY_ATOMS or total_atoms > MAX_TOTAL_ATOMS:
        raise MonomerDftRequestError(
            f"molecule exceeds the supported size limit ({MAX_HEAVY_ATOMS} heavy atoms, {MAX_TOTAL_ATOMS} total atoms)"
        )
    properties = _requested_properties(request)
    if {"hessian", "frequencies"}.intersection(properties) and total_atoms > MAX_HESSIAN_ATOMS:
        raise MonomerDftRequestError(f"Hessian calculations are limited to {MAX_HESSIAN_ATOMS} total atoms")

    _validate_model_domain(
        request,
        electron_count=nuclear_charge - effective_charge,
        effective_charge=effective_charge,
        atomic_numbers=set(atomic_numbers),
    )
    public_request = request.model_dump(mode="json")
    worker_request = public_request
    return PreparedMonomerDftRequest(
        public_request=public_request,
        worker_request=worker_request,
        request_sha256=calculation_request_sha256(worker_request),
        canonical_smiles=canonical_smiles,
        input_type=input_type,
        formal_charge=formal_charge,
        effective_charge=effective_charge,
        electron_count=nuclear_charge - effective_charge,
        atomic_numbers=atomic_numbers,
        isotope_mass_numbers=isotope_mass_numbers,
        atomic_masses_u=atomic_masses_u,
        total_atoms=total_atoms,
        heavy_atoms=heavy_atoms,
        warnings=tuple(warnings),
    )


def _formal_charge(smiles: str) -> int:
    Chem = _load_rdkit()
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:  # guarded by _canonicalize_and_validate_molecule
        raise MonomerDftRequestError("input.smiles is not a valid SMILES string")
    return int(Chem.GetFormalCharge(molecule))
