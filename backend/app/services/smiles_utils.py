from __future__ import annotations

from rdkit import Chem
from rdkit.Chem.MolStandardize import rdMolStandardize
from rdkit import RDLogger


RDLogger.DisableLog("rdApp.error")


def _parse_mol(smiles: str) -> Chem.Mol:
    value = smiles.strip()
    if not value:
        raise ValueError("smiles must not be empty")

    mol = Chem.MolFromSmiles(value)
    if mol is None:
        raise ValueError(f"invalid smiles: {smiles}")
    return mol


def normalize(smiles: str) -> str:
    return standardize_smiles(smiles)


def standardize_smiles(smiles: str) -> str:
    mol = _parse_mol(smiles)
    try:
        standardized = rdMolStandardize.Cleanup(mol)
        Chem.SanitizeMol(standardized)
    except Exception as exc:
        raise ValueError(f"failed to standardize smiles: {smiles}") from exc
    return Chem.MolToSmiles(standardized, canonical=True, isomericSmiles=True)


def are_equivalent(smiles1: str, smiles2: str) -> bool:
    return normalize(smiles1) == normalize(smiles2)
