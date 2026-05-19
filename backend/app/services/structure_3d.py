from __future__ import annotations

import multiprocessing
import queue
import time
from rdkit import Chem
from rdkit.Chem import AllChem

from app.utils.exceptions import InvalidSmilesError


def _cap_polymer_ends(smiles: str) -> Chem.Mol:
    mol = Chem.MolFromSmiles(smiles.strip())
    if mol is None:
        raise InvalidSmilesError(f"invalid smiles: {smiles}")

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
    except Exception as exc:
        raise InvalidSmilesError("failed to sanitize capped structure") from exc
    return capped


def _generate_3d_molblock_inner(smiles: str) -> tuple[str, str]:
    capped = _cap_polymer_ends(smiles)
    capped_smiles = Chem.MolToSmiles(capped, canonical=True)

    mol_3d = Chem.AddHs(capped)
    params = AllChem.ETKDGv3()
    params.randomSeed = 0xF00D
    embed_status = AllChem.EmbedMolecule(mol_3d, params)
    if embed_status != 0:
        raise InvalidSmilesError("failed to generate 3D coordinates")

    try:
        AllChem.UFFOptimizeMolecule(mol_3d, maxIters=500)
    except Exception:
        try:
            AllChem.MMFFOptimizeMolecule(mol_3d, maxIters=500)
        except Exception:
            pass

    return Chem.MolToMolBlock(mol_3d), capped_smiles


def _generate_3d_worker(smiles: str, queue: multiprocessing.Queue) -> None:
    try:
        queue.put(("ok", _generate_3d_molblock_inner(smiles)))
    except InvalidSmilesError as exc:
        queue.put(("invalid", str(exc)))
    except Exception:
        queue.put(("failed", "failed to generate 3D coordinates"))


def generate_3d_molblock(smiles: str, *, timeout_seconds: float = 8.0) -> tuple[str, str]:
    context = multiprocessing.get_context("spawn")
    result_queue: multiprocessing.Queue = context.Queue()
    process = context.Process(target=_generate_3d_worker, args=(smiles, result_queue))
    process.start()

    result: tuple[str, tuple[str, str] | str] | None = None
    deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
    while result is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.terminate()
            process.join()
            raise InvalidSmilesError("3D generation timed out")

        try:
            result = result_queue.get(timeout=min(0.1, remaining))
        except queue.Empty:
            if not process.is_alive():
                process.join()
                raise InvalidSmilesError("failed to generate 3D coordinates")

    process.join(timeout=1)
    if process.is_alive():
        process.terminate()
        process.join()
        raise InvalidSmilesError("failed to generate 3D coordinates")

    if process.exitcode != 0:
        raise InvalidSmilesError("failed to generate 3D coordinates")

    status, payload = result
    if status == "ok":
        return payload
    raise InvalidSmilesError(payload)
