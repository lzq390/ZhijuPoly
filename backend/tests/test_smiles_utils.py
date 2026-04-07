from __future__ import annotations

import pytest

from app.services.fingerprint import generate, tanimoto
from app.services.smiles_utils import are_equivalent, normalize


def test_normalize_returns_canonical_smiles() -> None:
    assert normalize(" C(C)O ") == "CCO"


def test_are_equivalent_accepts_reordered_smiles() -> None:
    assert are_equivalent("C(C)O", "OCC") is True


def test_normalize_supports_polymer_attachment_points() -> None:
    assert normalize("*CC*") == "*CC*"


def test_normalize_rejects_invalid_smiles() -> None:
    with pytest.raises(ValueError):
        normalize("not-a-smiles")


def test_generate_and_tanimoto() -> None:
    fp1 = generate("CCO")
    fp2 = generate("OCC")
    fp3 = generate("CCN")

    assert tanimoto(fp1, fp2) == 1.0
    assert 0.0 <= tanimoto(fp1, fp3) < 1.0
