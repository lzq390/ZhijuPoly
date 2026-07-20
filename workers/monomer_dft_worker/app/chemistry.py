from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .schemas import ModelAlias, MolecularInput


STANDARD_ELEMENTS = frozenset({1, 5, 6, 7, 8, 9, 14, 15, 16, 17, 33, 34, 35, 53})
PD_ELEMENTS = frozenset({1, 5, 6, 7, 8, 9, 14, 15, 16, 17, 34, 35, 46, 53})
RXN_ELEMENTS = frozenset({1, 6, 7, 8})
MIN_NET_CHARGE = -5
MAX_NET_CHARGE = 5

MODEL_DOMAINS: dict[str, dict[str, Any]] = {
    "aimnet2": {
        "family": "wb97m-d3",
        "elements": STANDARD_ELEMENTS,
        "supports_charge": True,
        "supports_open_shell": False,
        "recommended": True,
    },
    "aimnet2-b973c": {
        "family": "b973c-d3",
        "elements": STANDARD_ELEMENTS,
        "supports_charge": True,
        "supports_open_shell": False,
        "deprecated_for_new_work": True,
    },
    "aimnet2-2025": {
        "family": "b973c-2025-d3",
        "elements": STANDARD_ELEMENTS,
        "supports_charge": True,
        "supports_open_shell": False,
        "recommended": True,
    },
    "aimnet2-nse": {
        "family": "nse",
        "elements": STANDARD_ELEMENTS,
        "supports_charge": True,
        "supports_open_shell": True,
    },
    "aimnet2-pd": {
        "family": "pd",
        "elements": PD_ELEMENTS,
        "supports_charge": True,
        "supports_open_shell": False,
        "implicit_solvation": "CPCM(THF) training reference",
    },
    "aimnet2-rxn": {
        "family": "rxn",
        "elements": RXN_ELEMENTS,
        "supports_charge": False,
        "supports_open_shell": False,
    },
}


class ChemistryValidationError(ValueError):
    def __init__(
        self, code: str, message: str, *, details: dict[str, Any] | None = None
    ):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class PreparedMolecule:
    input_type: str
    canonical_smiles: str
    atomic_numbers: np.ndarray
    atomic_masses_u: np.ndarray
    isotope_mass_numbers: np.ndarray
    symbols: tuple[str, ...]
    coordinates_angstrom: np.ndarray
    rdkit_force_field: str
    rdkit_optimization_performed: bool
    rdkit_optimization_status: int
    formal_charge: int
    net_charge: int
    electron_count: int


def _validate_effective_charge(charge: int, *, inferred: bool) -> int:
    charge = int(charge)
    if charge < MIN_NET_CHARGE or charge > MAX_NET_CHARGE:
        raise ChemistryValidationError(
            "charge_out_of_range",
            "the effective molecular net charge must be between -5 and 5",
            details={"charge": charge, "inferred_from_formal_charge": inferred},
        )
    return charge


def _rdkit_modules():
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError as exc:  # pragma: no cover - runtime lock includes RDKit.
        raise RuntimeError("RDKit is not installed in the monomer DFT worker") from exc
    return Chem, AllChem


def _atomic_mass_metadata(molecule: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Resolve explicit isotopes without silently substituting natural masses.

    RDKit's ``Atom.GetMass`` returns the literal isotope label for unknown
    isotopes (for example ``[999C]`` -> 999.0), so it is not suitable for
    scientific mass weighting.  ``GetMassForIsotope`` returns zero for an
    isotope absent from RDKit's table, which we reject before queue admission.
    """
    Chem, _ = _rdkit_modules()
    table = Chem.GetPeriodicTable()
    atomic_numbers: list[int] = []
    isotope_mass_numbers: list[int] = []
    atomic_masses_u: list[float] = []
    for atom in molecule.GetAtoms():
        atomic_number = int(atom.GetAtomicNum())
        isotope_mass_number = int(atom.GetIsotope())
        if isotope_mass_number > 0:
            try:
                mass_u = float(
                    table.GetMassForIsotope(atomic_number, isotope_mass_number)
                )
            except Exception:
                mass_u = 0.0
            if not np.isfinite(mass_u) or mass_u <= 0.0:
                raise ChemistryValidationError(
                    "unsupported_isotope",
                    "an explicitly labelled isotope is unavailable in the RDKit mass table",
                    details={
                        "atomic_number": atomic_number,
                        "isotope_mass_number": isotope_mass_number,
                    },
                )
        else:
            mass_u = float(table.GetAtomicWeight(atomic_number))
            if not np.isfinite(mass_u) or mass_u <= 0.0:
                raise ChemistryValidationError(
                    "unsupported_isotope",
                    "an atomic mass is unavailable in the RDKit mass table",
                    details={"atomic_number": atomic_number},
                )
        atomic_numbers.append(atomic_number)
        isotope_mass_numbers.append(isotope_mass_number)
        atomic_masses_u.append(mass_u)
    return (
        np.asarray(atomic_numbers, dtype=np.int64),
        np.asarray(atomic_masses_u, dtype=np.float64),
        np.asarray(isotope_mass_numbers, dtype=np.int64),
    )


def parse_smiles_or_psmiles(text: str, psmiles_mode: str | None):
    Chem, _ = _rdkit_modules()
    if "." in text:
        raise ChemistryValidationError(
            "multi_fragment_input",
            "SMILES/PSMILES must describe exactly one connected molecular unit",
        )
    mol = Chem.MolFromSmiles(text)
    if mol is None:
        raise ChemistryValidationError(
            "invalid_smiles", "RDKit could not parse the SMILES/PSMILES input"
        )

    dummy_indices = [
        atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0
    ]
    if not dummy_indices:
        if psmiles_mode is not None:
            raise ChemistryValidationError(
                "invalid_psmiles_mode",
                "psmiles_mode must be null when the input has no '*' attachment marker",
            )
        return mol, "smiles"
    if psmiles_mode is None:
        raise ChemistryValidationError(
            "psmiles_mode_required",
            "psmiles_mode is required when the input contains '*' attachment markers",
        )
    if psmiles_mode == "close" and len(dummy_indices) != 2:
        raise ChemistryValidationError(
            "invalid_psmiles",
            "PSMILES close mode requires exactly two '*' attachment markers",
            details={"attachment_markers": len(dummy_indices)},
        )

    boundary_indices: list[int] = []
    bond_types: list[Any] = []
    for dummy_index in dummy_indices:
        dummy = mol.GetAtomWithIdx(dummy_index)
        neighbors = list(dummy.GetNeighbors())
        if len(neighbors) != 1:
            raise ChemistryValidationError(
                "invalid_psmiles",
                "each PSMILES attachment marker must have exactly one neighbor",
            )
        neighbor_index = neighbors[0].GetIdx()
        boundary_indices.append(neighbor_index)
        bond = mol.GetBondBetweenAtoms(dummy_index, neighbor_index)
        if bond is None:
            raise ChemistryValidationError(
                "invalid_psmiles", "attachment marker bond is missing"
            )
        bond_types.append(bond.GetBondType())

    if psmiles_mode == "close" and boundary_indices[0] == boundary_indices[1]:
        raise ChemistryValidationError(
            "invalid_psmiles",
            "the two PSMILES attachment markers cannot share one boundary atom",
        )

    editable = Chem.RWMol(mol)
    if psmiles_mode == "close":
        if bond_types[0] != bond_types[1]:
            raise ChemistryValidationError(
                "invalid_psmiles",
                "close mode requires matching attachment bond types",
            )
        if editable.GetBondBetweenAtoms(*boundary_indices) is None:
            bond_type = bond_types[0]
            if bond_type == Chem.BondType.UNSPECIFIED:
                bond_type = Chem.BondType.SINGLE
            editable.AddBond(boundary_indices[0], boundary_indices[1], bond_type)

    for dummy_index in sorted(dummy_indices, reverse=True):
        editable.RemoveAtom(dummy_index)
    cleaned = editable.GetMol()
    try:
        Chem.SanitizeMol(cleaned)
    except Exception as exc:
        raise ChemistryValidationError(
            "invalid_psmiles",
            f"PSMILES could not be sanitized after {psmiles_mode} processing",
        ) from exc
    fragments = Chem.GetMolFrags(cleaned)
    if len(fragments) != 1:
        raise ChemistryValidationError(
            "multi_fragment_input",
            "processed PSMILES did not produce one connected molecule",
        )
    return cleaned, f"psmiles_{psmiles_mode}"


def validate_model_domain(
    model: ModelAlias | str,
    atomic_numbers: np.ndarray,
    *,
    charge: int,
    multiplicity: int,
) -> int:
    charge = _validate_effective_charge(charge, inferred=False)
    domain = MODEL_DOMAINS.get(str(model))
    if domain is None:
        raise ChemistryValidationError(
            "unsupported_model", f"unsupported AIMNet2 model: {model}"
        )

    present = {int(number) for number in np.asarray(atomic_numbers).reshape(-1)}
    unsupported = sorted(present - set(domain["elements"]))
    if unsupported:
        raise ChemistryValidationError(
            "unsupported_element",
            f"model {model} does not support one or more elements",
            details={"atomic_numbers": unsupported},
        )
    if charge != 0 and not bool(domain["supports_charge"]):
        raise ChemistryValidationError(
            "unsupported_charge",
            f"model {model} only supports net-neutral systems",
            details={"charge": charge},
        )
    if multiplicity != 1 and not bool(domain["supports_open_shell"]):
        raise ChemistryValidationError(
            "unsupported_multiplicity",
            "multiplicity greater than one requires model 'aimnet2-nse'",
            details={"model": model, "multiplicity": multiplicity},
        )

    electron_count = int(np.asarray(atomic_numbers, dtype=np.int64).sum()) - charge
    if electron_count < 1:
        raise ChemistryValidationError(
            "invalid_electron_count",
            "charge produces a non-positive electron count",
            details={"electron_count": electron_count},
        )
    unpaired = multiplicity - 1
    if unpaired > electron_count or (electron_count - unpaired) % 2 != 0:
        raise ChemistryValidationError(
            "charge_multiplicity_mismatch",
            "charge and multiplicity are inconsistent with the molecular electron count",
            details={"electron_count": electron_count, "multiplicity": multiplicity},
        )
    return electron_count


def validate_request_chemistry(
    molecular_input: MolecularInput,
    model: ModelAlias | str,
    *,
    requires_hessian: bool = False,
) -> dict[str, Any]:
    """Perform cheap, deterministic validation before admitting a queued job."""
    Chem, _ = _rdkit_modules()
    molecule, input_type = parse_smiles_or_psmiles(
        molecular_input.smiles,
        molecular_input.psmiles_mode,
    )
    formal_charge = int(Chem.GetFormalCharge(molecule))
    net_charge = (
        formal_charge
        if molecular_input.net_charge is None
        else molecular_input.net_charge
    )
    net_charge = _validate_effective_charge(
        net_charge,
        inferred=molecular_input.net_charge is None,
    )
    with_hydrogens = Chem.AddHs(molecule)
    heavy_atom_count = int(molecule.GetNumHeavyAtoms())
    atom_count = int(with_hydrogens.GetNumAtoms())
    if heavy_atom_count > 100 or atom_count > 300:
        raise ChemistryValidationError(
            "molecule_too_large",
            "the worker accepts at most 100 heavy atoms and 300 total atoms",
            details={"heavy_atom_count": heavy_atom_count, "atom_count": atom_count},
        )
    if requires_hessian and atom_count > 100:
        raise ChemistryValidationError(
            "hessian_molecule_too_large",
            "Hessian and frequency jobs accept at most 100 total atoms",
            details={"atom_count": atom_count},
        )
    atomic_numbers, _, _ = _atomic_mass_metadata(with_hydrogens)
    electron_count = validate_model_domain(
        model,
        atomic_numbers,
        charge=net_charge,
        multiplicity=molecular_input.multiplicity,
    )
    return {
        "input_type": input_type,
        "canonical_smiles": Chem.MolToSmiles(molecule, canonical=True),
        "formal_charge": formal_charge,
        "net_charge": net_charge,
        "electron_count": electron_count,
        "heavy_atom_count": heavy_atom_count,
        "atom_count": atom_count,
        "atomic_numbers": atomic_numbers.tolist(),
    }


def prepare_molecule(
    molecular_input: MolecularInput, *, seed: int, max_iters: int
) -> PreparedMolecule:
    Chem, AllChem = _rdkit_modules()
    mol, input_type = parse_smiles_or_psmiles(
        molecular_input.smiles,
        molecular_input.psmiles_mode,
    )
    canonical_smiles = Chem.MolToSmiles(mol, canonical=True)
    formal_charge = int(Chem.GetFormalCharge(mol))
    net_charge = (
        formal_charge
        if molecular_input.net_charge is None
        else molecular_input.net_charge
    )
    net_charge = _validate_effective_charge(
        net_charge,
        inferred=molecular_input.net_charge is None,
    )
    with_hydrogens = Chem.AddHs(mol)

    params = AllChem.ETKDGv3()
    params.randomSeed = int(seed)
    params.clearConfs = True
    embed_status = int(AllChem.EmbedMolecule(with_hydrogens, params))
    if embed_status != 0:
        embed_status = int(
            AllChem.EmbedMolecule(
                with_hydrogens,
                randomSeed=int(seed),
                useRandomCoords=True,
                clearConfs=True,
            )
        )
    if embed_status != 0:
        raise ChemistryValidationError(
            "conformer_generation_failed",
            "RDKit could not generate a three-dimensional conformer",
            details={"embed_status": embed_status},
        )

    try:
        if AllChem.MMFFHasAllMoleculeParams(with_hydrogens):
            force_field = "MMFF94"
            optimization_status = int(
                AllChem.MMFFOptimizeMolecule(
                    with_hydrogens,
                    mmffVariant="MMFF94",
                    maxIters=int(max_iters),
                )
            )
        elif AllChem.UFFHasAllMoleculeParams(with_hydrogens):
            force_field = "UFF"
            optimization_status = int(
                AllChem.UFFOptimizeMolecule(with_hydrogens, maxIters=int(max_iters))
            )
        else:
            # ETKDG already produced a finite 3D conformer. Some supported AIMNet
            # domains (notably Pd) have no RDKit MMFF/UFF parameters, so retain
            # that geometry while recording that no force-field relaxation ran.
            force_field = "ETKDG-only"
            optimization_status = -1
    except ChemistryValidationError:
        raise
    except Exception as exc:
        raise ChemistryValidationError(
            "conformer_optimization_failed",
            "RDKit force-field conformer optimization failed",
        ) from exc

    conformer = with_hydrogens.GetConformer()
    numbers, atomic_masses_u, isotope_mass_numbers = _atomic_mass_metadata(
        with_hydrogens
    )
    coordinates = np.asarray(
        [
            [
                conformer.GetAtomPosition(index).x,
                conformer.GetAtomPosition(index).y,
                conformer.GetAtomPosition(index).z,
            ]
            for index in range(with_hydrogens.GetNumAtoms())
        ],
        dtype=np.float64,
    )
    if not np.isfinite(coordinates).all():
        raise ChemistryValidationError(
            "non_finite_geometry",
            "RDKit generated non-finite coordinates",
        )
    electron_count = int(numbers.sum()) - net_charge
    periodic_table = Chem.GetPeriodicTable()
    symbols = tuple(periodic_table.GetElementSymbol(int(number)) for number in numbers)
    return PreparedMolecule(
        input_type=input_type,
        canonical_smiles=canonical_smiles,
        atomic_numbers=numbers,
        atomic_masses_u=atomic_masses_u,
        isotope_mass_numbers=isotope_mass_numbers,
        symbols=symbols,
        coordinates_angstrom=coordinates,
        rdkit_force_field=force_field,
        rdkit_optimization_performed=force_field != "ETKDG-only",
        rdkit_optimization_status=optimization_status,
        formal_charge=formal_charge,
        net_charge=net_charge,
        electron_count=electron_count,
    )


def model_capabilities() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for alias, domain in MODEL_DOMAINS.items():
        item = {key: value for key, value in domain.items() if key != "elements"}
        item.update(
            {
                "alias": alias,
                "atomic_numbers": sorted(domain["elements"]),
                "elements": [
                    atomic_symbol(number) for number in sorted(domain["elements"])
                ],
            }
        )
        result.append(item)
    return result


def atomic_symbol(number: int) -> str:
    Chem, _ = _rdkit_modules()
    return str(Chem.GetPeriodicTable().GetElementSymbol(int(number)))
