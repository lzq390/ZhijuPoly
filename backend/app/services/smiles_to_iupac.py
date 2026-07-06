from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rdkit import Chem

from app.utils.exceptions import InvalidSmilesError


DASH_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"


class IupacNameLookupAmbiguousError(RuntimeError):
    """Raised when a cached IUPAC name resolves to more than one SMILES."""


@dataclass(frozen=True)
class IupacSmilesMatch:
    iupac_name: str
    smiles: str


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


def normalize_iupac_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value.strip())
    for dash in DASH_CHARS:
        text = text.replace(dash, "-")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    return text.casefold().strip()


def _find_normalized_name(normalized_text: str, normalized_name: str) -> int:
    start = 0
    while True:
        position = normalized_text.find(normalized_name, start)
        if position < 0:
            return -1
        if _has_name_boundaries(normalized_text, position, len(normalized_name)):
            return position
        start = position + 1


def _has_name_boundaries(text: str, position: int, length: int) -> bool:
    before = text[position - 1] if position > 0 else ""
    after_index = position + length
    after = text[after_index] if after_index < len(text) else ""
    return not _is_ascii_word_char(before) and not _is_ascii_word_char(after)


def _is_ascii_word_char(value: str) -> bool:
    return bool(value) and value.isascii() and value.isalnum()