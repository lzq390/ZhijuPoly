from __future__ import annotations

from rdkit import Chem
from rdkit.Chem import Draw


def generate_2d_svg(smiles: str) -> str | None:
    if not smiles.strip():
        return None

    try:
        mol = Chem.MolFromSmiles(smiles.strip())
    except (TypeError, ValueError):
        return None

    if mol is None:
        return None

    try:
        return Draw.MolsToGridImage(
            [mol],
            molsPerRow=1,
            subImgSize=(320, 220),
            useSVG=True,
        )
    except (RuntimeError, ValueError):
        return None
