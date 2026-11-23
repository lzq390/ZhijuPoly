"""Basic RDKit descriptors of a drawn structure, shown beside the structure editor.

Polymer repeat units mark their connection points with ``*`` dummy atoms. Those
atoms are counted separately and kept out of the formula and atom counts, so the
values describe the repeat unit itself.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors

from app.utils.exceptions import InvalidSmilesError


@dataclass(frozen=True, slots=True)
class StructureDescriptors:
    canonical_smiles: str
    formula: str
    molecular_weight: float
    heavy_atom_count: int
    attachment_point_count: int
    ring_count: int
    aromatic_ring_count: int
    rotatable_bond_count: int
    hbond_donor_count: int
    hbond_acceptor_count: int
    heteroatom_count: int
    tpsa: float
    fraction_csp3: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


_DUMMY_IN_FORMULA = re.compile(r"\*\d*")


def calculate_structure_descriptors(smiles: str) -> StructureDescriptors:
    value = smiles.strip()
    mol = Chem.MolFromSmiles(value) if value else None
    if mol is None or mol.GetNumAtoms() == 0:
        raise InvalidSmilesError(f"invalid smiles: {smiles}")

    attachment_points = sum(1 for atom in mol.GetAtoms() if atom.GetAtomicNum() == 0)
    return StructureDescriptors(
        canonical_smiles=Chem.MolToSmiles(mol, canonical=True),
        formula=_DUMMY_IN_FORMULA.sub("", rdMolDescriptors.CalcMolFormula(mol)),
        molecular_weight=round(float(Descriptors.MolWt(mol)), 3),
        heavy_atom_count=int(mol.GetNumHeavyAtoms()),
        attachment_point_count=attachment_points,
        ring_count=int(rdMolDescriptors.CalcNumRings(mol)),
        aromatic_ring_count=int(rdMolDescriptors.CalcNumAromaticRings(mol)),
        rotatable_bond_count=int(rdMolDescriptors.CalcNumRotatableBonds(mol)),
        hbond_donor_count=int(rdMolDescriptors.CalcNumHBD(mol)),
        hbond_acceptor_count=int(rdMolDescriptors.CalcNumHBA(mol)),
        # RDKit counts dummy atoms as heteroatoms; connection points are not.
        heteroatom_count=int(rdMolDescriptors.CalcNumHeteroatoms(mol)) - attachment_points,
        tpsa=round(float(rdMolDescriptors.CalcTPSA(mol)), 2),
        fraction_csp3=round(float(rdMolDescriptors.CalcFractionCSP3(mol)), 3),
    )
