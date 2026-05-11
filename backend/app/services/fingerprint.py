from __future__ import annotations

from rdkit import DataStructs
from rdkit.Chem import rdFingerprintGenerator
from rdkit.DataStructs.cDataStructs import ExplicitBitVect

from app.services.smiles_utils import _parse_mol


DEFAULT_RADIUS = 2
DEFAULT_NUM_BITS = 2048

_GENERATOR = rdFingerprintGenerator.GetMorganGenerator(
    radius=DEFAULT_RADIUS,
    fpSize=DEFAULT_NUM_BITS,
)


def generate(smiles: str) -> ExplicitBitVect:
    mol = _parse_mol(smiles)
    return _GENERATOR.GetFingerprint(mol)


def tanimoto(fp1: ExplicitBitVect, fp2: ExplicitBitVect) -> float:
    return float(DataStructs.TanimotoSimilarity(fp1, fp2))


def fingerprint_to_bytes(fp: ExplicitBitVect) -> bytes:
    return bytes(DataStructs.BitVectToBinaryText(fp))


def fingerprint_from_bytes(data: bytes | bytearray | memoryview) -> ExplicitBitVect:
    return DataStructs.CreateFromBinaryText(bytes(data))
