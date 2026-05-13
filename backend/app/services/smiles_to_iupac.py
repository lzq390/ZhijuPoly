from __future__ import annotations

import sqlite3

from rdkit import Chem

from app.pi_database import ensure_pi_schema
from app.utils.exceptions import InvalidSmilesError


def prepare_monomer_smiles_for_iupac(smiles: str) -> str:
    normalized = smiles.strip()
    if not normalized:
        raise InvalidSmilesError("monomer smiles must not be empty")

    mol = Chem.MolFromSmiles(normalized)
    if mol is None:
        raise InvalidSmilesError(f"invalid monomer smiles: {smiles}")

    editable = Chem.RWMol(mol)
    for atom in editable.GetAtoms():
        if atom.GetAtomicNum() == 0:
            atom.SetAtomicNum(1)
            atom.SetFormalCharge(0)
            atom.SetIsAromatic(False)
            atom.SetNoImplicit(True)

    capped = editable.GetMol()
    try:
        Chem.SanitizeMol(capped)
        capped = Chem.RemoveHs(capped, sanitize=True)
    except Exception as exc:
        raise InvalidSmilesError("failed to sanitize capped monomer smiles") from exc

    return Chem.MolToSmiles(capped, canonical=True)


def _cache_lookup_keys(smiles: str) -> list[str]:
    normalized = smiles.strip()
    if not normalized:
        return []

    try:
        prepared = prepare_monomer_smiles_for_iupac(normalized)
    except InvalidSmilesError:
        return [normalized]

    if prepared == normalized:
        return [prepared]
    return [prepared, normalized]


def lookup_iupac_name(connection: sqlite3.Connection, smiles: str) -> str | None:
    lookup_keys = _cache_lookup_keys(smiles)
    if not lookup_keys:
        return None

    ensure_pi_schema(connection)
    for lookup_key in lookup_keys:
        row = connection.execute(
            """
            SELECT iupac_name
            FROM smiles_iupac_cache
            WHERE smiles = ?
            """,
            (lookup_key,),
        ).fetchone()
        if row is None:
            continue

        value = row["iupac_name"]
        if value is None:
            return None
        return str(value).strip() or None

    return None


def cache_iupac_name(connection: sqlite3.Connection, smiles: str, iupac_name: str | None) -> None:
    lookup_keys = _cache_lookup_keys(smiles)
    if not lookup_keys:
        return

    ensure_pi_schema(connection)
    connection.execute(
        """
        INSERT OR REPLACE INTO smiles_iupac_cache (smiles, iupac_name)
        VALUES (?, ?)
        """,
        (lookup_keys[0], iupac_name.strip() if iupac_name else None),
    )
