from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass

from rdkit import Chem

from app.pi_database import ensure_pi_schema
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


def normalize_iupac_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value.strip())
    for dash in DASH_CHARS:
        text = text.replace(dash, "-")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s*-\s*", "-", text)
    return text.casefold().strip()


def lookup_smiles_by_iupac_name(connection: sqlite3.Connection, iupac_name: str) -> str | None:
    target = normalize_iupac_name(iupac_name)
    if not target:
        return None

    matches = _cached_iupac_matches(connection, target)
    if not matches:
        return None
    if len(matches) > 1:
        raise IupacNameLookupAmbiguousError(
            f"cached IUPAC name maps to multiple SMILES: {iupac_name}"
        )
    return matches[0].smiles


def find_iupac_smiles_matches(connection: sqlite3.Connection, text: str) -> list[IupacSmilesMatch]:
    normalized_text = normalize_iupac_name(text)
    if not normalized_text:
        return []

    candidates: list[tuple[int, int, IupacSmilesMatch]] = []
    for normalized_name, matches in _cached_iupac_match_groups(connection).items():
        position = _find_normalized_name(normalized_text, normalized_name)
        if position < 0:
            continue
        if len(matches) > 1:
            display_name = matches[0].iupac_name
            raise IupacNameLookupAmbiguousError(
                f"cached IUPAC name maps to multiple SMILES: {display_name}"
            )
        candidates.append((position, len(normalized_name), matches[0]))

    candidates.sort(key=lambda item: (-item[1], item[0]))
    selected: list[tuple[int, int, IupacSmilesMatch]] = []
    for position, length, match in candidates:
        span = (position, position + length)
        if any(
            _spans_overlap(span, (start, start + selected_length))
            for start, selected_length, _ in selected
        ):
            continue
        selected.append((position, length, match))

    selected.sort(key=lambda item: item[0])
    return [match for _, _, match in selected]


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


def _cached_iupac_matches(connection: sqlite3.Connection, normalized_name: str) -> list[IupacSmilesMatch]:
    return _cached_iupac_match_groups(connection).get(normalized_name, [])


def _cached_iupac_match_groups(connection: sqlite3.Connection) -> dict[str, list[IupacSmilesMatch]]:
    ensure_pi_schema(connection)
    rows = connection.execute(
        """
        SELECT smiles, iupac_name
        FROM smiles_iupac_cache
        WHERE iupac_name IS NOT NULL
        """
    ).fetchall()

    grouped: dict[str, list[IupacSmilesMatch]] = {}
    seen_by_name: dict[str, set[str]] = {}
    for row in rows:
        smiles = str(row["smiles"] or "").strip()
        iupac_name = str(row["iupac_name"] or "").strip()
        normalized_name = normalize_iupac_name(iupac_name)
        if not smiles or not normalized_name:
            continue

        seen_smiles = seen_by_name.setdefault(normalized_name, set())
        if smiles in seen_smiles:
            continue

        seen_smiles.add(smiles)
        grouped.setdefault(normalized_name, []).append(
            IupacSmilesMatch(iupac_name=iupac_name, smiles=smiles)
        )

    return grouped


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


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]
