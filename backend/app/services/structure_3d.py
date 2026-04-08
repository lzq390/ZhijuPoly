from __future__ import annotations

import multiprocessing
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


def generate_3d_molblock(smiles: str) -> tuple[str, str]:
    context = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue = context.Queue()
    process = context.Process(target=_generate_3d_worker, args=(smiles, queue))
    process.start()
    process.join(timeout=8)

    if process.is_alive():
        process.terminate()
        process.join()
        raise InvalidSmilesError("3D generation timed out")

    if process.exitcode != 0:
        raise InvalidSmilesError("failed to generate 3D coordinates")

    if queue.empty():
        raise InvalidSmilesError("failed to generate 3D coordinates")

    status, payload = queue.get()
    if status == "ok":
        return payload
    raise InvalidSmilesError(payload)
